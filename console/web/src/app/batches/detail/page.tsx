"use client";
import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import BatchWorkspace from "@/components/batches/BatchWorkspace";
import { useI18n } from "@/lib/i18n";
function Detail() {
  const id = useSearchParams().get("id");
  const { locale } = useI18n();
  if (!id)
    return (
      <div className="panel p-5">
        <Link href="/batches">
          {locale === "en" ? "Choose a batch" : "请选择批次"} →
        </Link>
      </div>
    );
  return <BatchWorkspace key={id} id={id} />;
}
export default function BatchDetailPage() {
  return (
    <React.Suspense fallback={null}>
      <Detail />
    </React.Suspense>
  );
}
