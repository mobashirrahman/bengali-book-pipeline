"""S1 family grouping, split assignment and probability sampling.

Pure, deterministic helpers over :mod:`pdf_craft_tool.research.schema`
records. No I/O except :func:`write_sample_manifest`; no network, no models.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path

from pdf_craft_tool.research import schema

_CERTAIN_SCORE = 0.98


@dataclass(frozen=True)
class Document:
    document_id: str
    content_sha256: str
    work_id: str = ""
    edition_id: str = ""
    processed: bool = False
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})


@dataclass(frozen=True)
class GroupEdge:
    a: str
    b: str
    relation: str
    method: str = ""
    score: float = 0.0
    confirmed: bool = False


@dataclass(frozen=True)
class Family:
    family_id: str
    members: tuple
    content_hashes: tuple
    processed_members: tuple
    needs_confirmation: tuple

    def __post_init__(self):
        object.__setattr__(self, "members", tuple(self.members))
        object.__setattr__(self, "content_hashes", tuple(self.content_hashes))
        object.__setattr__(self, "processed_members",
                           tuple(self.processed_members))
        object.__setattr__(self, "needs_confirmation",
                           tuple(self.needs_confirmation))


def _family_id(member_ids) -> str:
    canon = "\0".join(sorted(member_ids))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def group_families(documents, edges, *, min_confirmed_score=0.0,
                   min_unconfirmed_score=0.90) -> list:
    """Union-find grouping over documents.

    An edge merges its endpoints when it is confirmed, or when it is a
    ``byte_identical`` edge whose content hashes are equal, or when its
    score reaches ``min_unconfirmed_score``. Applied edges with
    ``0 < score < 0.98`` are still flagged in ``needs_confirmation``;
    advisory-only edges are not merged but are flagged on both endpoints'
    families.
    """
    docs = {}
    for doc in documents or []:
        docs[doc.document_id] = doc
    parent = {key: key for key in docs}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(first, second):
        root_a, root_b = find(first), find(second)
        if root_a != root_b:
            parent[root_b] = root_a

    applied_flags: list = []
    advisory: list = []
    for edge in edges or []:
        if edge.a not in docs or edge.b not in docs or edge.a == edge.b:
            continue
        if edge.confirmed and edge.score >= min_confirmed_score:
            merges = True
        elif (edge.relation == "byte_identical" and docs[edge.a]
                .content_sha256 == docs[edge.b].content_sha256):
            merges = True
        elif edge.score >= min_unconfirmed_score:
            merges = True
        else:
            merges = False
        pair = tuple(sorted((edge.a, edge.b)))
        if merges:
            union(edge.a, edge.b)
            if 0 < edge.score < _CERTAIN_SCORE:
                applied_flags.append(pair)
        else:
            advisory.append(pair)

    groups: dict = {}
    for doc_id in docs:
        groups.setdefault(find(doc_id), []).append(doc_id)
    fam_of = {}
    needs: dict = {}
    for members in groups.values():
        fid = _family_id(members)
        for member in members:
            fam_of[member] = fid
        needs[fid] = set()
    for pair in applied_flags:
        needs[fam_of[find(pair[0])]].add(pair)
    for pair in advisory:
        needs[fam_of[find(pair[0])]].add(pair)
        needs[fam_of[find(pair[1])]].add(pair)

    families = []
    for members in groups.values():
        ordered = tuple(sorted(members))
        fid = _family_id(ordered)
        families.append(Family(
            family_id=fid,
            members=ordered,
            content_hashes=tuple(docs[mid].content_sha256
                                 for mid in ordered),
            processed_members=tuple(mid for mid in ordered
                                    if docs[mid].processed),
            needs_confirmation=tuple(sorted(needs[fid])),
        ))
    families.sort(key=lambda fam: fam.family_id)
    return families


def assign_splits(families, allocation: dict, *, seed: int) -> dict:
    """Assign each family to a split, deterministically.

    Families are ordered by ``sha256(f"{seed}\\\\0{family_id}")`` and each
    split (visited in sorted-name order) is filled to its quota. Leftover
    families map to ``'unassigned'``. Every member of a family shares the
    family's split: the mapping is keyed by ``family_id``.
    """
    quotas = {name: max(int(count), 0)
              for name, count in dict(allocation or {}).items()}
    remaining = dict(quotas)
    order = sorted(quotas)

    def sort_key(family) -> str:
        blob = f"{seed}\0{family.family_id}".encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    mapping: dict = {}
    for family in sorted(families or [], key=sort_key):
        chosen = "unassigned"
        for name in order:
            if remaining[name] > 0:
                chosen = name
                remaining[name] -= 1
                break
        mapping[family.family_id] = chosen
    return mapping


@dataclass(frozen=True)
class SamplingResult:
    manifest: "schema.Manifest"
    pages: tuple
    challenge_pages: tuple

    def __post_init__(self):
        object.__setattr__(self, "pages", tuple(self.pages))
        object.__setattr__(self, "challenge_pages",
                           tuple(self.challenge_pages))


def _family_seed(seed: int, family_id: str) -> int:
    digest = hashlib.sha256(family_id.encode("utf-8")).hexdigest()
    return seed ^ int(digest, 16)


def probability_sample(*, families, split_of, pages_available: dict,
                       pages_per_family: int, seed: int,
                       stratum_of: dict,
                       schema_version=schema.SCHEMA_VERSION) -> SamplingResult:
    """Draw ``pages_per_family`` pages per family with a seeded RNG.

    ``selection_probability`` is ``pages_per_family`` divided by the total
    available pages in the family, clamped to ``(0, 1]``. Families whose
    processed members have zero available pages are skipped. Families with
    no processed members use a synthetic ``[1..pages_per_family]`` page
    list and a ``'+unprocessed'`` stratum suffix so they are never
    silently dropped.
    """
    if pages_per_family < 1:
        raise ValueError("pages_per_family must be >= 1")
    available = dict(pages_available or {})
    splits = dict(split_of or {})
    strata = dict(stratum_of or {})
    records: list = []
    _valid_splits = {member.value for member in schema.Split}
    for family in sorted(families or [], key=lambda fam: fam.family_id):
        # A family that assign_splits left over ('unassigned') or that carries
        # no / an unknown split is not sampled -- it is not a schema.Split, so
        # building a SamplePage from it would raise mid-loop.
        if splits.get(family.family_id) not in _valid_splits:
            continue
        hash_of = dict(zip(family.members, family.content_hashes))
        processed_with = [mid for mid in family.processed_members
                          if available.get(mid)]
        total = sum(len(available[mid]) for mid in processed_with)
        suffix = ""
        if total == 0:
            if family.processed_members or not family.members:
                continue
            first = sorted(family.members)[0]
            pool = [(first, num)
                    for num in range(1, pages_per_family + 1)]
            total = len(pool)
            suffix = "+unprocessed"
        else:
            pool = [(mid, num) for mid in processed_with
                    for num in available[mid]]
        take = min(pages_per_family, len(pool))
        rng = random.Random(_family_seed(seed, family.family_id))
        chosen = rng.sample(pool, take)
        probability = min(1.0, pages_per_family / total)
        if not 0 < probability <= 1:
            continue
        split = splits[family.family_id]
        stratum = strata.get(family.family_id, "unstratified") + suffix
        for member, page_number in chosen:
            source = hash_of.get(member, "")
            page_id = schema.record_hash({
                "source_sha256": source,
                "page_number": page_number,
                "page_number_convention": "pdf_index",
            })
            records.append(schema.SamplePage(
                page_id=page_id,
                source_sha256=source,
                work_id="unknown",
                edition_id="unknown",
                overlap_group=family.family_id,
                split=split,
                stratum=stratum,
                selection_probability=probability,
                seed=seed,
                manifest_version=schema.MANIFEST_VERSION,
                schema_version=schema_version,
                file_hashes={},
                legacy_gold_ids=(),
            ).to_dict())
    manifest = schema.Manifest.build("sample", records)
    return SamplingResult(manifest=manifest, pages=tuple(records),
                          challenge_pages=())


def challenge_sample(*, candidates, exclude_page_ids=(), seed: int = 0,
                     max_pages: int | None = None,
                     schema_version=schema.SCHEMA_VERSION) -> tuple:
    """Re-stamp error-enriched candidate pages with split ``'challenge'``.

    Candidates whose ``page_id`` is in ``exclude_page_ids`` (e.g. the
    probability sample) are dropped so the challenge set never overlaps
    the prevalence sample.
    """
    excluded = set(exclude_page_ids or [])
    stamped: list = []
    for candidate in candidates or []:
        if isinstance(candidate, schema.SamplePage):
            record = candidate.to_dict()
        else:
            record = dict(candidate)
        if record.get("page_id") in excluded:
            continue
        record["split"] = "challenge"
        if "schema_version" in record:
            record["schema_version"] = schema_version
        stamped.append(schema.SamplePage.from_dict(record).to_dict())
    if max_pages is not None and len(stamped) > max_pages:
        stamped = random.Random(seed).sample(stamped, max_pages)
    return tuple(stamped)


def write_sample_manifest(result: SamplingResult, path: Path) -> None:
    """Persist ``result.manifest`` immutably via ``schema.Manifest.write``.

    A different manifest (e.g. from another seed) already present at
    ``path`` raises ``schema.ContractError`` instead of overwriting.
    """
    result.manifest.write(Path(path))
