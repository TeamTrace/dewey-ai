import os
from dataclasses import dataclass


@dataclass
class AzureOpenAIConfig:
    endpoint: str
    api_key: str
    embedding_deployment: str
    embedding_model: str
    chat_deployment: str
    chat_model: str

@dataclass
class AzureSearchConfig:
    """
    Azure Config Helper.

    ## Paramaters
    **service_endpoint**: *str* The URL to your Azure AI Search resource.

    index_name: str
    key: str
    """
    service_endpoint: str
    index_name: str
    key: str


@dataclass
class NewsroomConfig:
    """
    Newsroom-specific prompt values. Defaults reproduce the original
    Philadelphia Inquirer deployment so existing setups behave the same.

    **publisher**: whose articles the archive contains.
    **archive_start**: human-readable earliest date in the archive.
    **default_region**: region to assume when a question names none.
        Empty string disables the assumption (e.g. for a national outlet).
    """
    publisher: str = "The Philadelphia Inquirer"
    archive_start: str = "January 2, 1978"
    default_region: str = "the Greater Philadelphia Region"

    @classmethod
    def from_env(cls) -> "NewsroomConfig":
        defaults = cls()
        return cls(
            publisher=os.getenv("NEWSROOM_NAME", defaults.publisher),
            archive_start=os.getenv("ARCHIVE_START_DATE", defaults.archive_start),
            default_region=os.getenv("DEFAULT_REGION", defaults.default_region),
        )
