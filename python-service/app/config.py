from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    # PostgreSQL
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "monitoring"
    POSTGRES_USER: str = "zabbix"
    POSTGRES_PASSWORD: str = "zabbix_secret_2024"

    # MQTT
    MQTT_BROKER: str = "mosquitto"
    MQTT_PORT: int = 1883
    MQTT_TOPIC: str = "sensors/#"
    MQTT_TOPIC_EXTRA: str = "zeta/#"

    # Zabbix
    ZABBIX_API_URL: str = "http://zabbix-web:8080/api_jsonrpc.php"
    ZABBIX_USER: str = "Admin"
    ZABBIX_PASSWORD: str = "zabbix"
    ZABBIX_SYNC_INTERVAL: int = 60
    ZABBIX_TRAPPER_HOST: str = "zabbix-server"
    ZABBIX_TRAPPER_PORT: int = 10051
    ZABBIX_TRAPPER_HOSTNAME: str = "Zabbix server"

    # Device discovery window (days)
    DEVICE_DISCOVERY_DAYS: int = 90

    # Zabbix host provisioning defaults
    ZABBIX_DEFAULT_GROUPS: str = "IoT Sensors"
    ZABBIX_DEFAULT_TEMPLATES: str = ""
    ZABBIX_DEFAULT_INTERFACE_TYPE: int = 1  # 1=agent
    ZABBIX_DEFAULT_PORT: int = 10050
    ZABBIX_AUTO_PROVISION_HOSTS: bool = True

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()