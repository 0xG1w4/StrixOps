"""One persistent interception CA per MCP data root, never per task.

The complete mitmproxy certificate store is atomically published before a
capture worker mounts it read-only. Existing shared material is validated and
never silently replaced, including when its certificate has expired.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class CertificateError(ValueError):
    """The persistent CA needs explicit repair; a replacement was not created."""


_LOCK = threading.RLock()
_KEY_FILE = "mitmproxy-ca.pem"
_CERT_FILE = "mitmproxy-ca-cert.pem"
_DH_FILE = "mitmproxy-dhparam.pem"
# PKCS#3 encoding of RFC 3526 section 3's public 2048-bit MODP parameters.
# Mitmproxy requires this file even when clients negotiate modern ECDHE TLS.
_DHPARAM = b"""-----BEGIN DH PARAMETERS-----
MIIBCAKCAQEA///////////JD9qiIWjCNMTGYouA3BzRKQJOCIpnzHQCC76mOxOb
IlFKCHmONATd75UZs806QxswKwpt8l8UN0/hNW1tUcJF5IW1dmJefsb0TELppjft
awv/XLb0Brft7jhr+1qJn6WunyQRfEsf5kkoZlHs5Fs9wgB8uKFjvwWY2kg2HFXT
mmkWP6j9JM9fg2VdI9yjrZYcYvNWIIVSu57VKQdwlpZtZww1Tkq8mATxdGwIyhgh
fDKQXkYuNs474553LBgOhgObJ4Oi7Aeij7XFXfBvTFLJ3ivL9pVYFxg5lUl86pVq
5RXSJhiY+gUQFXKOWoqsqmj//////////wIBAg==
-----END DH PARAMETERS-----
"""


def _read(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
        raise CertificateError("CA files must be regular files smaller than 128 KiB")
    return path.read_bytes()


def _pair(directory: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Validate the signing pair and the exact public certificate being served."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise CertificateError("CA directory must be a real directory")
        combined = _read(directory / _KEY_FILE)
        key = serialization.load_pem_private_key(combined, password=None)
        certificates = x509.load_pem_x509_certificates(combined)
        if len(certificates) != 1 or not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise CertificateError("CA requires one certificate and an RSA signing key of at least 2048 bits")
        certificate = certificates[0]
        public_pem = _read(directory / _CERT_FILE)
        if b"PRIVATE KEY" in public_pem:
            raise CertificateError("The public CA certificate contains private key material")
        public = x509.load_pem_x509_certificates(public_pem)
        if len(public) != 1 or public[0] != certificate:
            raise CertificateError("Public CA certificate does not match the signing certificate")
        if key.public_key().public_numbers() != certificate.public_key().public_numbers():
            raise CertificateError("CA private key does not match its certificate")
        certificate.verify_directly_issued_by(certificate)
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
        if not constraints.critical or not constraints.value.ca or not usage.value.key_cert_sign:
            raise CertificateError("Certificate is not a valid certificate-signing authority")
        now = datetime.now(UTC)
        if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
            raise CertificateError(
                "Shared CA certificate is expired or not yet valid; explicit repair is required"
            )
        return key, certificate
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError("CA signing key or certificate is missing, invalid, or inconsistent") from exc


def _generate() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "StrixOps MCP Local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "StrixOps"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_store(directory: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    pair = _pair(directory)
    try:
        if _read(directory / _DH_FILE) != _DHPARAM:
            raise CertificateError("Shared CA has invalid DH parameters")
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise CertificateError("Shared CA contains an unexpected non-file entry")
            path.chmod(0o600)
        directory.chmod(0o700)
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(
            "Shared CA store is incomplete or invalid; explicit repair is required"
        ) from exc
    return pair


def _candidates(root: Path, preferred: Iterable[Path]) -> list[Path]:
    discovered = list(root.glob("tasks/*/captures/*/ca"))

    def modified(path: Path) -> float:
        try:
            return (path / _KEY_FILE).stat().st_mtime
        except OSError:
            return 0

    ordered = [Path(path) for path in preferred] + sorted(discovered, key=modified, reverse=True)
    candidates = []
    for path in ordered:
        if not path.is_symlink() and path.resolve().is_relative_to(root / "tasks") and path not in candidates:
            candidates.append(path)
    return candidates


def ensure_ca(root: Path, legacy_candidates: Iterable[Path] = ()) -> Path:
    """Return root/ca; migrate the first valid preferred/newest legacy CA once.

    A thread lock and advisory file lock serialize creators across API processes.
    If root/ca already exists, corruption never causes an implicit key rotation.
    """
    try:
        return _ensure_ca(root, legacy_candidates)
    except OSError as exc:
        # Service errors are public. Retain the underlying filesystem details
        # only on the exception chain, never in the API-visible message.
        raise CertificateError(
            "Cannot access shared CA storage; check storage permissions and available disk space"
        ) from exc


def _ensure_ca(root: Path, legacy_candidates: Iterable[Path]) -> Path:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    directory = root / "ca"
    with _LOCK:
        descriptor = os.open(root / ".ca.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if directory.exists() or directory.is_symlink():
                _validate_store(directory)
                return directory
            pair = None
            adopted = False
            for candidate in _candidates(root, legacy_candidates):
                try:
                    pair = _pair(candidate)
                    adopted = True
                    break
                except CertificateError:
                    continue
            key, certificate = pair or _generate()
            temporary = Path(tempfile.mkdtemp(prefix=".ca-", dir=root))
            try:
                temporary.chmod(0o700)
                pem = certificate.public_bytes(serialization.Encoding.PEM)
                _write(
                    temporary / _KEY_FILE,
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.TraditionalOpenSSL,
                        serialization.NoEncryption(),
                    )
                    + pem,
                )
                _write(temporary / _CERT_FILE, pem)
                _write(temporary / "mitmproxy-ca-cert.cer", pem)
                _write(
                    temporary / "mitmproxy-ca-cert.p12",
                    pkcs12.serialize_key_and_certificates(
                        b"StrixOps MCP CA",
                        None,
                        certificate,
                        None,
                        serialization.NoEncryption(),
                    ),
                )
                _write(temporary / _DH_FILE, _DHPARAM)
                _write(
                    temporary / "metadata.json",
                    json.dumps(
                        {
                            "legacy_adopted": adopted,
                            "created_at": datetime.now(UTC).isoformat(),
                            "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
                        }
                    ).encode(),
                )
                _validate_store(temporary)
                _sync_directory(temporary)
                temporary.rename(directory)
                _sync_directory(root)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return directory


def ca_info(directory: Path) -> dict:
    """Public identity and lifetime only; never returns private material or paths."""
    _, certificate = _pair(Path(directory))
    metadata = {}
    try:
        parsed = json.loads(_read(Path(directory) / "metadata.json"))
        metadata = parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        pass
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    return {
        "sha256": fingerprint,
        "fingerprint_sha256": fingerprint,
        "subject": certificate.subject.rfc4514_string(),
        "not_before": certificate.not_valid_before_utc.isoformat(),
        "not_after": certificate.not_valid_after_utc.isoformat(),
        "scope": "instance",
        "legacy_adopted": bool(metadata.get("legacy_adopted", False)),
    }
