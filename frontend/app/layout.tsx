import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Archivage Zimbra",
  description: "Téléchargez un dossier de votre messagerie Zimbra au format ZIP",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr">
      <body>
        <header className="site-header">
          <span className="site-title">Archivage Zimbra</span>
        </header>
        <main className="site-main">{children}</main>
      </body>
    </html>
  );
}
