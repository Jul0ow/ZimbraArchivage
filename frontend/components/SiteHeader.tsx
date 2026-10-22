type Props = {
  // Utilisateur connecté, absent sur la page de connexion
  username?: string | null;
  onLogout?: () => void;
};

export default function SiteHeader({ username, onLogout }: Props) {
  return (
    <header className="site-header">
      <span className="site-title">Archivage Zimbra</span>
      {username && (
        <div className="user-menu">
          <span className="muted">
            Connecté en tant que <strong>{username}</strong>
          </span>
          <button type="button" className="button small" onClick={onLogout}>
            Se déconnecter
          </button>
        </div>
      )}
    </header>
  );
}
