import pathlib
from abc import ABC, abstractmethod

class Pipeline(ABC):
    # every pipeline must define:
    pipeline_name: str
    pipeline_description: str
    docs_path: pathlib.Path

    @abstractmethod
    def run(self) -> None: ...
