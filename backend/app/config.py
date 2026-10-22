"""Configuration de l'application, lue depuis les variables d'environnement."""

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # URL de l'API SOAP, de la forme https://domaine.com/service/soap
    zimbra_soap_url: str
    # URL de l'API ReST, de la forme https://domaine.com/service
    zimbra_rest_url: str

    # Fuseau horaire utilisé pour les filtres de dates et le nommage des fichiers .eml
    zone_id: str = "Europe/Paris"
    # Nombre de messages remontés par page de SearchRequest
    search_batch_size: int = Field(default=500, ge=1, le=1000)
    # Longueur maximale des noms de fichiers .eml (bornée comme dans Archivage)
    max_file_name_length: int = 50
    # Délai maximal d'un appel HTTP vers Zimbra, en secondes
    http_timeout: float = 120.0
    # Vérifier le certificat TLS du serveur Zimbra
    verify_tls: bool = True

    # Attribut Secure du cookie de session (à désactiver uniquement en développement HTTP)
    cookie_secure: bool = True

    @field_validator("zimbra_soap_url", "zimbra_rest_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("max_file_name_length")
    @classmethod
    def clamp_file_name_length(cls, value: int) -> int:
        return max(50, min(value, 180))

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.zone_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
