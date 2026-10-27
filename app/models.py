from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    document_key: str
    case_number: str
    decision_date: str
    document_type: str
    title: str
    url: str
    registry_id: str

