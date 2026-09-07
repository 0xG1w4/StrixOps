"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { ArrowDownToLine, Check, ChevronDown, Cpu, Eye, EyeOff, Globe, KeyRound, Loader2, Search, Server, X } from "lucide-react";
import { fetchModelCatalog, ModelCatalogError, type CatalogModel } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./ModelRouteDialog.module.css";

export const OPENROUTER_BASE = "https://openrouter.ai/api/v1";
export type RouteType = "custom" | "openrouter";
export interface ModelRouteDraft {
  id: string | null;
  name: string;
  route_type: RouteType;
  llm_api_base: string;
  llm_api_key: string;
  keyVisible: boolean;
  model_web: string;
  model_internal: string;
}

const COPY = {
  "zh-CN": {
    subtitle: "连接模型服务，为 Web 与内网扫描分配模型。",
    connection: "连接服务",
    assignment: "选择模型", assignmentHint: "从服务商清单选择，也可以直接输入模型 ID。",
    openrouter: "一个接口，连接多个模型服务商", custom: "OpenAI 兼容网关或本地服务",
    fetch: "拉取模型清单", fetching: "正在拉取…", refresh: "重新拉取",
    fetchHint: "填写 API 地址和密钥后，即可读取可用模型。",
    ready: (count: number) => `已获取 ${count} 个模型`,
    web: "Web 扫描", internal: "内网扫描", optional: "至少填写一项",
    choose: "选择模型", search: "搜索模型名称或 ID…", noMatches: "没有匹配的模型，试试其他关键词。",
    pickerHint: "先拉取模型清单，也可直接输入模型 ID。",
    modelPlaceholder: "输入模型 ID，或从右侧选择",
    footer: "保存后可在启动扫描时使用此路由。",
    savedKey: "已保存的密钥可直接拉取；输入新密钥即可替换。",
    errors: {
      invalid_route: "请选择有效的模型服务类型。",
      invalid_api_base: "请输入有效的 HTTP / HTTPS API 地址，不含查询参数或账号密码。",
      profile_not_found: "这条模型路由已不存在，请关闭窗口后重试。",
      api_key_required: "请先填写 API 密钥。",
      endpoint_changed: "服务地址已改变，请重新输入该服务的 API 密钥。",
      upstream_auth: "密钥验证失败，请检查密钥与服务地址。",
      upstream_timeout: "服务响应超时，请稍后重试。",
      upstream_connection: "无法连接模型服务，请检查地址与网络。",
      upstream_redirect: "该地址要求跳转，请直接填写最终 API 地址。",
      upstream_http: "服务未能返回模型清单，请确认地址支持 /models。",
      invalid_response: "返回内容不是有效的模型清单，可继续手动输入模型 ID。",
      empty_models: "服务返回了空清单，可继续手动输入模型 ID。",
      console_endpoint_missing: "后端尚未提供模型清单接口，请重启或更新后端后重试。",
      console_connection: "无法连接控制台后端，请确认后端正在运行并检查网络连接。",
      console_invalid_response: "控制台后端返回了无效响应，请重试或检查后端服务。",
      console_http_error: "控制台后端请求失败，请检查后端服务后重试。",
      unavailable: "暂时无法拉取模型，请重试或手动输入模型 ID。",
    } as Record<string, string>,
  },
  en: {
    subtitle: "Connect a provider and assign models to Web and internal scans.",
    connection: "Connection",
    assignment: "Choose models", assignmentHint: "Choose from the provider catalog or enter a model ID directly.",
    openrouter: "One API for multiple model providers", custom: "OpenAI-compatible gateways or local services",
    fetch: "Fetch models", fetching: "Fetching…", refresh: "Refresh models",
    fetchHint: "Enter an API base and key to load available models.",
    ready: (count: number) => `${count} models available`,
    web: "Web scanning", internal: "Internal scanning", optional: "At least one required",
    choose: "Choose model", search: "Search model name or ID…", noMatches: "No matching models. Try another search.",
    pickerHint: "Fetch models first, or enter a model ID directly.",
    modelPlaceholder: "Enter or choose a model ID",
    footer: "This route will be available when you launch a scan.",
    savedKey: "Use your saved key to fetch models, or enter a new key to replace it.",
    errors: {
      invalid_route: "Choose a valid provider type.",
      invalid_api_base: "Enter a valid HTTP / HTTPS API base without query parameters or credentials.",
      profile_not_found: "This route no longer exists. Close the dialog and try again.",
      api_key_required: "Enter an API key first.",
      endpoint_changed: "The service URL changed. Enter the API key for this service again.",
      upstream_auth: "Authentication failed. Check the API key and service URL.",
      upstream_timeout: "The service timed out. Please try again.",
      upstream_connection: "Cannot connect to the provider. Check the URL and network.",
      upstream_redirect: "This URL redirects. Enter the final API base directly.",
      upstream_http: "The provider could not return models. Check that the URL supports /models.",
      invalid_response: "The response is not a valid model catalog. You can still enter IDs manually.",
      empty_models: "The catalog is empty. You can still enter model IDs manually.",
      console_endpoint_missing: "The backend does not provide the model catalog endpoint yet. Restart or update it and try again.",
      console_connection: "Cannot connect to the console backend. Check that it is running and your connection is available.",
      console_invalid_response: "The console backend returned an invalid response. Try again or check the backend service.",
      console_http_error: "The console backend request failed. Check the backend service and try again.",
      unavailable: "Unable to fetch models. Try again or enter model IDs manually.",
    } as Record<string, string>,
  },
};

type Copy = (typeof COPY)["en"];

function ModelField({ id, label, Icon, value, onChange, models, disabled, copy }: {
  id: string; label: string; Icon: typeof Globe; value: string;
  onChange: (value: string) => void; models: CatalogModel[]; disabled: boolean; copy: Copy;
}) {
  const [open, setOpen] = React.useState(false);
  const [search, setSearch] = React.useState("");
  const menuRef = React.useRef<HTMLDivElement>(null);
  const searchRef = React.useRef<HTMLInputElement>(null);
  const query = search.trim().toLowerCase();
  const filtered = React.useMemo(() => models.filter((model) =>
    model.id.toLowerCase().includes(query) || model.name.toLowerCase().includes(query),
  ), [models, query]);

  React.useEffect(() => { if (!models.length || disabled) setOpen(false); }, [models, disabled]);
  React.useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => searchRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  return (
    <div className={styles.modelCard}>
      <label className={styles.modelLabel} htmlFor={id}><Icon size={16} />{label}</label>
      <div className={styles.inputGroup}>
        <input id={id} className={styles.modelInput} value={value} disabled={disabled}
          placeholder={copy.modelPlaceholder} onChange={(event) => onChange(event.target.value)}
          autoComplete="off" spellCheck={false} />
        {/* The portaled menu needs its own scroll lock inside the modal dialog. */}
        <Menu.Root modal open={open} onOpenChange={(next) => { setOpen(next); if (next) setSearch(""); }}>
          <Menu.Trigger asChild>
            <button type="button" className={styles.pickerTrigger} disabled={disabled || !models.length}
              aria-label={`${copy.choose} · ${label}`} title={models.length ? copy.choose : copy.pickerHint}>
              <ChevronDown size={17} />
            </button>
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content ref={menuRef} align="end" sideOffset={8} collisionPadding={16}
              className={styles.modelMenu} aria-label={`${copy.choose} · ${label}`}
              onKeyDown={(event) => {
                if (event.target === searchRef.current) return;
                const first = menuRef.current?.querySelector('[role="menuitemradio"]');
                if ((event.key === "Tab" && event.shiftKey) || (event.key === "ArrowUp" && event.target === first)) {
                  event.preventDefault();
                  event.stopPropagation();
                  searchRef.current?.focus();
                }
              }}>
              <div className={styles.searchBox}>
                <Search size={15} aria-hidden="true" />
                <input ref={searchRef} value={search} placeholder={copy.search} aria-label={copy.search}
                  onChange={(event) => setSearch(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") return;
                    event.stopPropagation();
                    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                      event.preventDefault();
                      const items = menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]');
                      const target = event.key === "ArrowDown" ? items?.[0] : items?.[items.length - 1];
                      target?.focus();
                    }
                  }} />
              </div>
              <div className={styles.modelMenuList}>
                <Menu.RadioGroup value={value} onValueChange={onChange}>
                  {filtered.map((model) => (
                    <Menu.RadioItem key={model.id} value={model.id} textValue={model.id} className={styles.modelOption}>
                      <span className={styles.modelOptionText}>
                        <span className={styles.modelId}>{model.id}</span>
                        {model.name !== model.id && <span className={styles.modelName}>{model.name}</span>}
                      </span>
                      <Menu.ItemIndicator><Check size={15} /></Menu.ItemIndicator>
                    </Menu.RadioItem>
                  ))}
                </Menu.RadioGroup>
                {!filtered.length && <p className={styles.noMatches} role="status">{copy.noMatches}</p>}
              </div>
              <div className={styles.menuCount}>{filtered.length} / {models.length}</div>
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
      </div>
    </div>
  );
}

export function ModelRouteDialog({ draft, onChange, onClose, onSave, errors, saving }: {
  draft: ModelRouteDraft; onChange: (draft: ModelRouteDraft) => void;
  onClose: () => void; onSave: () => void; errors: string[]; saving: boolean;
}) {
  const { t, locale } = useI18n();
  const copy = COPY[locale];
  const [models, setModels] = React.useState<CatalogModel[]>([]);
  const [fetching, setFetching] = React.useState(false);
  const [fetchError, setFetchError] = React.useState("");
  const nameRef = React.useRef<HTMLInputElement>(null);
  const pending = React.useRef<AbortController | null>(null);
  const initialEndpoint = React.useRef({ route: draft.route_type, base: draft.llm_api_base.trim().replace(/\/+$/, "") });
  const base = draft.route_type === "openrouter" ? OPENROUTER_BASE : draft.llm_api_base.trim();
  const savedKey = !!draft.id && (!draft.llm_api_key.trim() || draft.llm_api_key.startsWith("•••"));
  const endpointChanged = savedKey && (draft.route_type !== initialEndpoint.current.route
    || base.replace(/\/+$/, "") !== initialEndpoint.current.base);
  const canFetch = !!base && (!!draft.llm_api_key.trim() || savedKey) && !endpointChanged;

  React.useEffect(() => {
    pending.current?.abort();
    setModels([]);
    setFetchError("");
    setFetching(false);
    return () => { pending.current?.abort(); };
  }, [draft.id, draft.route_type, base, draft.llm_api_key]);

  const fetchModels = async () => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setFetching(true);
    setFetchError("");
    try {
      const result = await fetchModelCatalog({ profile_id: draft.id, route_type: draft.route_type,
        llm_api_base: base, llm_api_key: draft.llm_api_key.trim() }, controller.signal);
      if (!controller.signal.aborted) {
        setModels(result.models);
        if (!result.models.length) setFetchError("empty_models");
      }
    } catch (error) {
      if (controller.signal.aborted || (error && typeof error === "object" && "name" in error && error.name === "AbortError")) return;
      setFetchError(error instanceof ModelCatalogError ? error.code : "unavailable");
    } finally {
      if (!controller.signal.aborted) setFetching(false);
    }
  };

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.dialog}
          onOpenAutoFocus={(event) => { event.preventDefault(); nameRef.current?.focus(); }}
          onEscapeKeyDown={(event) => { if (saving) event.preventDefault(); }}
          onInteractOutside={(event) => { if (saving) event.preventDefault(); }}>
          <header className={styles.header}>
            <div className={styles.headerIcon}><Cpu size={22} /></div>
            <div className={styles.heading}>
              <Dialog.Title className={styles.title}>{t(draft.id ? "settings.editProfile" : "settings.newProfile")}</Dialog.Title>
              <Dialog.Description className={styles.subtitle}>{copy.subtitle}</Dialog.Description>
            </div>
            <Dialog.Close asChild><button type="button" className={styles.close} disabled={saving} aria-label={t("common.close")}><X size={19} /></button></Dialog.Close>
          </header>
          <form className={styles.form} onSubmit={(event) => { event.preventDefault(); if (!saving) onSave(); }}>
            <div className={styles.body}>
              <div className={styles.field}>
                <label htmlFor="profile-name">{t("settings.profileName")}</label>
                <input ref={nameRef} id="profile-name" className={styles.input} placeholder={t("settings.profileName.placeholder")}
                  value={draft.name} disabled={saving} onChange={(event) => onChange({ ...draft, name: event.target.value })} />
              </div>
              <section className={styles.section} aria-labelledby="route-connection-title">
                <div className={styles.sectionHeading}>
                  <span className={styles.step}>01</span>
                  <div><h3 id="route-connection-title">{copy.connection}</h3></div>
                </div>
                <div className={styles.providers} role="group" aria-label={copy.connection}>
                  {(["openrouter", "custom"] as RouteType[]).map((route) => {
                    const selected = draft.route_type === route;
                    const Icon = route === "openrouter" ? Globe : Server;
                    return <button key={route} type="button" className={styles.provider} aria-pressed={selected} disabled={saving}
                      onClick={() => { if (!selected) onChange({ ...draft, route_type: route, llm_api_base: route === "openrouter" ? OPENROUTER_BASE : "" }); }}>
                      <span className={styles.providerTitle}><Icon size={17} />{route === "openrouter" ? "OpenRouter" : t("settings.customEndpoint")}<span className={styles.radio}>{selected && <span />}</span></span>
                      <span className={styles.providerCopy}>{route === "openrouter" ? copy.openrouter : copy.custom}</span>
                    </button>;
                  })}
                </div>
                <div className={styles.credentials}>
                  <div className={styles.field}>
                    <label htmlFor="profile-api-base">{t("settings.apiBase")}{draft.route_type === "openrouter" && <span className={styles.fieldBadge}>OpenRouter</span>}</label>
                    <input id="profile-api-base" className={`${styles.input} ${styles.mono}`} disabled={saving || draft.route_type === "openrouter"}
                      spellCheck={false} autoComplete="off" placeholder="https://api.example.com/v1" value={base}
                      onChange={(event) => onChange({ ...draft, llm_api_base: event.target.value })} />
                  </div>
                  <div className={styles.field}>
                    <label htmlFor="profile-api-key">{t("settings.apiKey")}</label>
                    <div className={styles.inputGroup}>
                      <KeyRound size={15} className={styles.keyIcon} aria-hidden="true" />
                      <input id="profile-api-key" className={`${styles.keyInput} ${styles.mono}`} type={draft.keyVisible ? "text" : "password"}
                        disabled={saving} autoComplete="off" spellCheck={false} placeholder={draft.id ? t("settings.keyUnchanged") : "sk-…"}
                        value={draft.llm_api_key} onChange={(event) => onChange({ ...draft, llm_api_key: event.target.value })} />
                      <button type="button" className={styles.iconButton} disabled={saving}
                        onClick={() => onChange({ ...draft, keyVisible: !draft.keyVisible })}
                        aria-label={t(draft.keyVisible ? "settings.hideKey" : "settings.showKey")}>
                        {draft.keyVisible ? <EyeOff size={16} /> : <Eye size={16} />}
                      </button>
                    </div>
                    {savedKey && !endpointChanged && <p className={styles.hint}>{copy.savedKey}</p>}
                  </div>
                </div>
                <div className={styles.fetchRow}>
                  <button type="button" className={styles.fetchButton} disabled={!canFetch || fetching || saving} onClick={() => void fetchModels()}>
                    {fetching ? <Loader2 size={16} className={styles.spin} /> : <ArrowDownToLine size={16} />}
                    {fetching ? copy.fetching : models.length ? copy.refresh : copy.fetch}
                  </button>
                  <span className={models.length ? styles.fetchSuccess : styles.hint} role="status" aria-live="polite">
                    {models.length ? <><Check size={14} />{copy.ready(models.length)}</> : copy.fetchHint}
                  </span>
                </div>
                {(fetchError || endpointChanged) && <p className={styles.error} role="alert">{copy.errors[endpointChanged ? "endpoint_changed" : fetchError] || copy.errors.unavailable}</p>}
              </section>
              <section className={styles.section} aria-labelledby="route-models-title">
                <div className={styles.sectionHeading}>
                  <span className={styles.step}>02</span>
                  <div><h3 id="route-models-title">{copy.assignment}</h3><p>{copy.assignmentHint}</p></div>
                  <span className={styles.optional}>{copy.optional}</span>
                </div>
                <div className={styles.models}>
                  <ModelField id="profile-web-model" label={copy.web} Icon={Globe} value={draft.model_web}
                    onChange={(value) => onChange({ ...draft, model_web: value })} models={models} disabled={saving || fetching} copy={copy} />
                  <ModelField id="profile-internal-model" label={copy.internal} Icon={Server} value={draft.model_internal}
                    onChange={(value) => onChange({ ...draft, model_internal: value })} models={models} disabled={saving || fetching} copy={copy} />
                </div>
              </section>
              {errors.length > 0 && <div className={styles.error} role="alert">{errors.map((error) => <div key={error}>{error}</div>)}</div>}
            </div>
            <footer className={styles.footer}>
              <p>{copy.footer}</p>
              <div className={styles.actions}>
                <button type="button" className="button-secondary" disabled={saving} onClick={onClose}>{t("common.cancel")}</button>
                <button type="submit" className="button-primary" disabled={saving}>
                  {saving ? <Loader2 size={15} className={styles.spin} /> : <Check size={15} />}
                  {t(draft.id ? "settings.saveChanges" : "settings.createProfile")}
                </button>
              </div>
            </footer>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
