import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Archivage Zimbra",
  description: "Téléchargez un dossier de votre messagerie Zimbra au format ZIP",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr">
      <body>{children}</body>
    </html>
  );
}
