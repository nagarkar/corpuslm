"""A model client for the tests: llmlm's, with corpuslm's reply validator and schema loader."""

from llmlm.llm import Client

from corpuslm import schemas


def client(*args, validator=None, schema_loader=None, **kwargs) -> Client:
    return Client(
        *args,
        validator=validator if validator is not None else schemas.validate,
        schema_loader=schema_loader if schema_loader is not None else schemas.schema,
        **kwargs,
    )
