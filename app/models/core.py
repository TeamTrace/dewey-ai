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


class NewsroomConfigError(ValueError):
    """NEWSROOM_NAME is unset or blank. The message is written for the person running Dewey."""


@dataclass
class NewsroomConfig:
    """
    Newsroom-specific values for prompts, tool descriptions and article labels.

    **publisher**: the newsroom whose archive this is. Required: it has no default, because a
        default would label one newsroom's articles as another's (Dewey used to fall back to
        The Philadelphia Inquirer, the newsroom it was first built for).
    **archive_start**: human-readable earliest date in the archive. Empty: no date is claimed.
    **default_region**: region to assume when a question names none.
        Empty string disables the assumption (e.g. for a national outlet).
    """
    publisher: str
    archive_start: str = ""
    default_region: str = ""

    @classmethod
    def from_env(cls) -> "NewsroomConfig":
        """Read NEWSROOM_NAME, ARCHIVE_START_DATE and DEFAULT_REGION. Blank counts as unset."""
        publisher = (os.getenv("NEWSROOM_NAME") or "").strip()
        if not publisher:
            raise NewsroomConfigError(
                "NEWSROOM_NAME is not set. Dewey labels your own articles with it and names it to AI "
                "tools, so it has no default. Add it to .env, for example NEWSROOM_NAME='Your Newsroom', "
                "or run python install.py and fill in the newsroom name."
            )
        return cls(
            publisher=publisher,
            archive_start=(os.getenv("ARCHIVE_START_DATE") or "").strip(),
            default_region=(os.getenv("DEFAULT_REGION") or "").strip(),
        )
