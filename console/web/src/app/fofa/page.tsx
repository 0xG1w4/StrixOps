"use client";
import * as React from "react";
import FofaWorkspace from "@/components/fofa/FofaWorkspace";
export default function FofaPage() {
  return (
    <React.Suspense fallback={null}>
      <FofaWorkspace />
    </React.Suspense>
  );
}
