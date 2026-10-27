"use client";

import { useRouter } from "next/navigation";
import { useCallback } from "react";

import { ApiError } from "@/lib/api";

/** Gestion commune des erreurs d'API : retour à la connexion si la session a expiré. */
export function useApiErrorHandler(setError: (message: string) => void): (e: unknown) => void {
  const router = useRouter();
  return useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) {
        router.replace("/");
        return;
      }
      setError(e instanceof ApiError ? e.message : "Erreur inattendue");
    },
    [router, setError],
  );
}
