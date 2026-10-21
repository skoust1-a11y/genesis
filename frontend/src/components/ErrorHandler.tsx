"use client";

import { useEffect } from "react";

export function ErrorHandler() {
  useEffect(() => {
    const handleRejection = (event: PromiseRejectionEvent) => {
      // If rejection reason is a DOM Event or extension event without stack, prevent Next.js dev overlay crash
      if (
        event.reason instanceof Event ||
        (event.reason &&
          typeof event.reason === "object" &&
          ("type" in event.reason || "target" in event.reason) &&
          !("stack" in event.reason))
      ) {
        event.preventDefault();
        console.warn("[TSL] Suppressed unhandled Event rejection:", event.reason);
      }
    };

    window.addEventListener("unhandledrejection", handleRejection);
    return () => window.removeEventListener("unhandledrejection", handleRejection);
  }, []);

  return null;
}
