from __future__ import annotations

import io
import json
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from google.api_core.exceptions import NotFound
from google.cloud import storage  # type: ignore[attr-defined, unused-ignore]
from google.cloud.storage import (  # type: ignore[attr-defined, unused-ignore, import-untyped]
    Blob,
    transfer_manager,
)
from langchain_core.documents import Document
from langchain_core.stores import BaseStore

if TYPE_CHECKING:
    from google.cloud import datastore  # type: ignore[attr-defined, unused-ignore]

GCS_MAX_BATCH_SIZE = 100


class DocumentStorage(BaseStore[str, Document]):
    """Abstract interface of a key, text storage for retrieving documents."""


class GCSDocumentStorage(DocumentStorage):
    """Stores documents in Google Cloud Storage.
    For each pair id, document_text the name of the blob will be {prefix}/{id} stored
    in plain text format.
    """

    def __init__(
        self,
        bucket: storage.Bucket,
        prefix: Optional[str] = "documents",
        threaded=True,
        n_threads=8,
    ) -> None:
        """Constructor.
        Args:
            bucket: Bucket where the documents will be stored.
            prefix: Prefix that is prepended to all document names.
        """
        super().__init__()
        self._bucket = bucket
        self._prefix = prefix
        self._threaded = threaded
        self._n_threads = n_threads
        if threaded:
            if not (int(n_threads) > 0 and int(n_threads) <= 50):
                raise ValueError(
                    "n_threads must be a valid integer,"
                    " greater than 0 and lower than or equal to 50"
                )

    def _prepare_doc_for_bulk_upload(
        self, key: str, value: Document
    ) -> Tuple[io.IOBase, Blob]:
        document_json = value.dict()
        document_text = json.dumps(document_json).encode("utf-8")
        doc_contents = io.BytesIO(document_text)
        blob_name = self._get_blob_name(key)
        blob = self._bucket.blob(blob_name)
        return doc_contents, blob

    def mset(self, key_value_pairs: Sequence[Tuple[str, Document]]) -> None:
        """Stores a series of documents using each keys

        Args:
            key_value_pairs (Sequence[Tuple[K, V]]): A sequence of key-value pairs.
        """
        if self._threaded:
            results = transfer_manager.upload_many(
                [
                    self._prepare_doc_for_bulk_upload(key, value)
                    for key, value in key_value_pairs
                ],
                skip_if_exists=False,
                upload_kwargs=None,
                deadline=None,
                raise_exception=False,
                worker_type="thread",
                max_workers=self._n_threads,
            )

            for result in results:
                # The results list is either `None` or an exception for each filename in
                # the input list, in order.
                if isinstance(result, Exception):
                    raise result
        else:
            for key, value in key_value_pairs:
                self._set_one(key, value)

    def _convert_bytes_to_doc(
        self, doc: io.BytesIO, result: Any
    ) -> Union[Document, None]:
        if isinstance(result, NotFound):
            return None
        elif result is None:
            doc.seek(0)
            raw_doc = doc.read()
            data = raw_doc.decode("utf-8")
            data_json = json.loads(data)
            return Document(**data_json)
        else:
            raise Exception(
                "Unexpected result type when batch getting multiple files from GCS"
            )

    def mget(self, keys: Sequence[str]) -> List[Optional[Document]]:
        """Gets a batch of documents by id.
        The default implementation only loops `get_by_id`.
        Subclasses that have faster ways to retrieve data by batch should implement
        this method.
        Args:
            ids: List of ids for the text.
        Returns:
            List of documents. If the key id is not found for any id record returns a
                None instead.
        """
        if self._threaded:
            download_docs = [
                (self._bucket.blob(self._get_blob_name(key)), io.BytesIO())
                for key in keys
            ]
            download_results = transfer_manager.download_many(
                download_docs,
                skip_if_exists=False,
                download_kwargs=None,
                deadline=None,
                raise_exception=False,
                worker_type="thread",
                max_workers=self._n_threads,
            )
            for i, result in enumerate(download_results):
                if isinstance(result, Exception) and not isinstance(result, NotFound):
                    raise result
            return [
                self._convert_bytes_to_doc(doc[1], result)
                for doc, result in zip(download_docs, download_results)
            ]
        else:
            return [self._get_one(key) for key in keys]

    def mdelete(self, keys: Sequence[str]) -> None:
        """Deletes a batch of documents by id.

        Args:
            keys: List of ids for the text.
        """
        for i in range(0, len(keys), GCS_MAX_BATCH_SIZE):
            batch = keys[i : i + GCS_MAX_BATCH_SIZE]
            with self._bucket.client.batch():
                for key in batch:
                    self._delete_one(key)

    def yield_keys(self, *, prefix: str | None = None) -> Iterator[str]:
        """Yields the keys present in the storage.

        Args:
            prefix: Ignored. Uses the prefix provided in the constructor.
        """
        for blob in self._bucket.list_blobs(prefix=self._prefix):
            yield blob.name.split("/")[-1]

    def _get_one(self, key: str) -> Document | None:
        """Gets the text of a document by its id. If not found, returns None.
        Args:
            key: Id of the document to get from the storage.
        Returns:
            Document if found, otherwise None.
        """

        blob_name = self._get_blob_name(key)
        existing_blob = self._bucket.get_blob(blob_name)

        if existing_blob is None:
            return None

        document_str = existing_blob.download_as_text()
        document_json: Dict[str, Any] = json.loads(document_str)
        return Document(**document_json)

    def _set_one(self, key: str, value: Document) -> None:
        """Stores a document text associated to a document_id.
        Args:
            key: Id of the document to be stored.
            document: Document to be stored.
        """
        blob_name = self._get_blob_name(key)
        new_blow = self._bucket.blob(blob_name)

        document_json = value.dict()
        document_text = json.dumps(document_json)
        new_blow.upload_from_string(document_text)

    def _delete_one(self, key: str) -> None:
        """Deletes one document by its key.

        Args:
            key (str): Id of the document to delete.
        """
        blob_name = self._get_blob_name(key)
        blob = self._bucket.blob(blob_name)
        blob.delete()

    def _get_blob_name(self, document_id: str) -> str:
        """Builds a blob name using the prefix and the document_id.
        Args:
            document_id: Id of the document.
        Returns:
            Name of the blob that the document will be/is stored in
        """
        return f"{self._prefix}/{document_id}"


class DataStoreDocumentStorage(DocumentStorage):
    """Stores documents in Google Cloud DataStore."""

    def __init__(
        self,
        datastore_client: datastore.Client,
        kind: str = "document_id",
        text_property_name: str = "text",
        metadata_property_name: str = "metadata",
    ) -> None:
        """Constructor.
        Args:
            bucket: Bucket where the documents will be stored.
            prefix: Prefix that is prepended to all document names.
        """
        super().__init__()
        self._client = datastore_client
        self._text_property_name = text_property_name
        self._metadata_property_name = metadata_property_name
        self._kind = kind

    def mget(self, keys: Sequence[str]) -> List[Optional[Document]]:
        """Gets a batch of documents by id.
        Args:
            ids: List of ids for the text.
        Returns:
            List of texts. If the key id is not found for any id record returns a None
                instead.
        """
        ds_keys = [self._client.key(self._kind, id_) for id_ in keys]

        entities = self._client.get_multi(ds_keys)

        # Entities are not sorted by key by default, the order is unclear. This orders
        # the list by the id retrieved.
        entity_id_lookup = {entity.key.id_or_name: entity for entity in entities}
        entities = [entity_id_lookup.get(id_) for id_ in keys]

        return [
            Document(
                page_content=entity[self._text_property_name],
                metadata=self._convert_entity_to_dict(
                    entity[self._metadata_property_name]
                ),
            )
            if entity is not None
            else None
            for entity in entities
        ]

    def mset(self, key_value_pairs: Sequence[Tuple[str, Document]]) -> None:
        """Stores a series of documents using each keys

        Args:
            key_value_pairs (Sequence[Tuple[K, V]]): A sequence of key-value pairs.
        """
        ids = [key for key, _ in key_value_pairs]
        documents = [document for _, document in key_value_pairs]

        with self._client.transaction():
            keys = [self._client.key(self._kind, id_) for id_ in ids]

            entities = []
            for key, document in zip(keys, documents):
                entity = self._client.entity(key=key, exclude_from_indexes=[self._text_property_name])
                entity[self._text_property_name] = document.page_content
                entity[self._metadata_property_name] = document.metadata
                entities.append(entity)

            self._client.put_multi(entities)

    def mdelete(self, keys: Sequence[str]) -> None:
        """Deletes a sequence of documents by key.

        Args:
            keys (Sequence[str]): A sequence of keys to delete.
        """
        with self._client.transaction():
            keys = [self._client.key(self._kind, id_) for id_ in keys]
            self._client.delete_multi(keys)

    def yield_keys(self, *, prefix: str | None = None) -> Iterator[str]:
        """Yields the keys of all documents in the storage.

        Args:
            prefix: Ignored
        """
        query = self._client.query(kind=self._kind)
        query.keys_only()
        for entity in query.fetch():
            yield str(entity.key.id_or_name)

    def _convert_entity_to_dict(self, entity: datastore.Entity) -> Dict[str, Any]:
        """Recursively transform an entity into a plain dictionary."""
        from google.cloud import datastore  # type: ignore[attr-defined, unused-ignore]

        dict_entity = dict(entity)
        for key in dict_entity:
            value = dict_entity[key]
            if isinstance(value, datastore.Entity):
                dict_entity[key] = self._convert_entity_to_dict(value)
        return dict_entity
