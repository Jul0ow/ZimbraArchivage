"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { ApiError, getMe, login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Déjà connecté : on passe directement au choix du dossier
  useEffect(() => {
    getMe()
      .then(() => router.replace("/dossiers"))
      .catch(() => undefined);
  }, [router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username.trim(), password);
      router.push("/dossiers");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Connexion impossible");
      setLoading(false);
    }
  }

  return (
    <section className="card login-card">
      <h1>Connexion</h1>
      <p className="muted">Connectez-vous avec votre compte de messagerie Zimbra.</p>
      <form onSubmit={handleSubmit} className="form">
        <label>
          Identifiant (adresse e-mail)
          <input
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label>
          Mot de passe
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="button primary" disabled={loading}>
          {loading ? "Connexion…" : "Se connecter"}
        </button>
      </form>
    </section>
  );
}
