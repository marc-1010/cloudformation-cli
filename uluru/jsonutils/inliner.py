import logging
from collections.abc import Iterable, Mapping

from jsonschema import RefResolver

from .renamer import RefRenamer
from .utils import BASE, rewrite_ref, traverse

LOG = logging.getLogger(__name__)


class RefInliner(RefResolver):
    """Mutates the schema."""

    def __init__(self, base_uri, schema):
        self.schema = schema
        self.ref_graph = {}

        try:
            existing_keys = set(self.schema["definitions"].keys())
        except (TypeError, KeyError, AttributeError):
            # TypeError: schema is not a dict/Mapping
            # KeyError: schema has no definitions
            # AttributeError: definitions is not a dict/Mapping
            existing_keys = set()

        self.renamer = RefRenamer(renames={base_uri: BASE}, banned=existing_keys)
        super().__init__(base_uri=base_uri, referrer=self.schema, cache_remote=True)

    def _walk_schema(self):
        self._walk(self.schema, (BASE,))

    def _walk(self, obj, old_path):
        if isinstance(obj, str):
            return  # very common, easier to debug this case

        if isinstance(obj, Mapping):
            for key, value in obj.items():
                if key == "$ref":
                    if old_path in self.ref_graph:
                        LOG.debug("Already visited '%s' (%s)", old_path, value)
                        return
                    url, resolved = self.resolve(value)
                    LOG.debug("Resolved '%s' to '%s'", value, url)
                    # parse the URL into
                    new_path = self.renamer.parse_ref_url(url)
                    LOG.debug("Parsed '%s' to '%s'", url, new_path)
                    LOG.debug("Edge from '%s' to '%s'", old_path, new_path)
                    self.ref_graph[old_path] = new_path
                    self.push_scope(url)
                    try:
                        self._walk(resolved, new_path)
                    finally:
                        self.pop_scope()
                else:
                    self._walk(value, old_path + (key,))
        # order matters, both Mapping and strings are also Iterable
        elif isinstance(obj, Iterable):
            for i, value in enumerate(obj):
                self._walk(value, old_path + (str(i),))
        # fall-through: for other types, there's nothing to do

    def _rewrite_refs(self):
        for base_uri, rename in self.renamer.items():
            LOG.debug("Rewriting refs in '%s' (%s)", rename, base_uri)
            document = self.store[base_uri]
            for from_ref, to_ref in self.ref_graph.items():
                # only process refs in this file
                if from_ref[0] != rename:
                    continue
                current = traverse(document, from_ref)
                new_ref = rewrite_ref(to_ref)
                LOG.debug("  '%s' -> '%s'", current["$ref"], new_ref)
                current["$ref"] = new_ref

    def _inline_defs(self):
        global_defs = self.schema.get("definitions", {})
        for base_uri, rename in self.renamer.items():
            if rename is BASE:  # no need to process the local file
                continue
            LOG.debug("Inlining definitions from '%s' (%s)", rename, base_uri)
            global_defs[rename] = local_defs = {"$comment": base_uri}
            document = self.store[base_uri]
            for to_ref in self.ref_graph.values():
                base, *parts = to_ref
                # only process refs in this file
                if base != rename:
                    continue
                # convert the parts into one flattened reference
                if parts:
                    key = "/".join(parts)
                    local_defs[key] = traverse(document, to_ref)
                    LOG.debug("  %s#%s", base, key)
                else:
                    local_defs.update(document)
        self.schema["definitions"] = global_defs

    def inline(self):
        self._walk_schema()
        self._rewrite_refs()
        self._inline_defs()
        return self.schema