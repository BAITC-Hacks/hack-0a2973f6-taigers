import json
from pathlib import Path


class TermsService:
    def __init__(self, path: str = "data/purchase_terms.json"):
        self.path = Path(path)

    def get(self, topic: str = "all") -> dict:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if topic == "all":
            return data
        return {topic: data.get(topic, "Информация требует уточнения у менеджера.")}
