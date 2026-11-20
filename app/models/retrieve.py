from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional


class DateRange(BaseModel):
    start_date: str = Field(..., description="start_date")
    end_date: str = Field(..., description="end_date")


class SearchParams(BaseModel):
    query: str = Field(..., description="query")
    date_range: DateRange = Field(..., description="date_range")
    authors: List[str] = Field(..., description="authors")


# Structured-output schema for the metadata step. Mirrors the `search_archive`
# tool in tools/retrieve.py so every provider returns the same dict shape:
#   {"question": str,
#    "date_range": {"start_date": str|None, "end_date": str|None},
#    "authors": [{"name": str}, ...]}
class SearchDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required but nullable, like the strict `search_archive` tool schema.
    start_date: Optional[str] = Field(
        ..., description="The start date of the user's query in ISO format (YYYY-MM-DD), or null."
    )
    end_date: Optional[str] = Field(
        ..., description="The end date of the user's query in ISO format (YYYY-MM-DD), or null."
    )


class SearchAuthor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Full journalist name")


class SearchMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., description="The semantic search query text to find relevant articles.")
    date_range: SearchDateRange
    authors: List[SearchAuthor]
