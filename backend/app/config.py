"""Configuration de l'application, lue depuis les variables d'environnement."""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # URL de l'API SOAP, de la forme https://domaine.com/service/soap
    zimbra_soap_url: str
    # URL de l'API ReST, de la forme https://domaine.com/service
    zimbra_rest_url: str

    # Fuseau horaire utilisé pour les filtres de dates et le nommage des fichiers .eml
    zone_id: str = "Europe/Paris"
    # Nombre de messages remontés par page de SearchRequest
    search_batch_size: int = Field(default=500, ge=1, le=1000)
    # Longueur maximale des noms de fichiers .eml (bornée comme dans Archivage)
    max_file_name_length: int = Field(default=50, ge=50, le=180)
    # Délai maximal d'un appel HTTP vers Zimbra, en secondes
    http_timeout: float = Field(default=120.0, gt=0)
    # Vérifier le certificat TLS du serveur Zimbra
    verify_tls: bool = True

    # Nombre de courriels téléchargés en parallèle pendant un export
    download_concurrency: int = Field(default=4, ge=1, le=16)
    # Exports et estimations simultanés, au total et par session
    max_concurrent_exports: int = Field(default=6, ge=1)
    max_exports_per_session: int = Field(default=2, ge=1)
    # Au-delà de cette taille (en octets), les pièces jointes d'un courriel ne sont pas extraites
    max_attachment_extraction_size: int = Field(default=25 * 1024 * 1024, ge=0)

    # Tentatives de connexion échouées tolérées sur 5 minutes, par identifiant
    max_login_failures_per_user: int = Field(default=5, ge=1)
    # Et par adresse IP (0 : pas de limite). À n'activer qu'avec FORWARDED_ALLOW_IPS limité aux
    # proxys de confiance : sinon tous les utilisateurs partagent l'adresse du proxy, ou
    # l'adresse peut être falsifiée par l'en-tête X-Forwarded-For
    max_login_failures_per_ip: int = Field(default=0, ge=0)

    # Attribut Secure du cookie de session (à désactiver uniquement en développement HTTP)
    cookie_secure: bool = True
    # Durée maximale d'une session, en secondes, quelle que soit la durée de vie du jeton Zimbra
    session_max_age: int = Field(default=8 * 3600, ge=60)
    # Clé de chiffrement du jeton Zimbra dans le cookie (Fernet, 32 octets en base64 urlsafe).
    # Sans clé, une clé aléatoire est générée au démarrage : les sessions ne survivent pas
    # à un redémarrage et ne sont pas partagées entre plusieurs réplicas.
    session_secret: str = ""

    @field_validator("zimbra_soap_url", "zimbra_rest_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("zone_id")
    @classmethod
    def check_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"fuseau horaire inconnu : {value}") from e
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.zone_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
