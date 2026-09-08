"""Fuse the duplicate signals into clusters and pick which copy to keep.

No single signal sees the whole problem.  Page images compare two PDFs but not a
PDF against an EPUB; text MinHash spans formats but only reaches the 17% of PDFs
with a text layer; normalized metadata reaches everything with a title but
cannot tell a duplicate file from a second edition.  Each signal therefore
proposes *edges*, and this module decides what the edges collectively mean.

The central point is that "these two files are the same book" is three different
findings, only one of which is a reason to remove anything:

``duplicate``
    Same book, same format, one of them redundant -- two scrapes of one PDF, or
    two scans of one book at different crop and DPI.  A keeper is chosen and the
    others become removal candidates.
``format_variant``
    Same book as a PDF *and* as an EPUB.  Both are kept: they are not competing
    copies, they are the scanned original and the reflowable reading copy.
``same_work``
    Same title and author, but the evidence is metadata alone, so this may well
    be two genuine printings.  Both are kept and linked.

Nothing here removes anything by itself.  The output is a set of tables
recording the judgement and the evidence behind it, which the read API can
filter and a human can overturn; acting on it is a separate, explicit step.
"""

from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Signals that compare the bytes or the words, rather than the description.
CONTENT_METHODS = frozenset({"sha256", "page_image", "text_minhash"})
METADATA_METHODS = frozenset({"metadata_exact", "metadata_fuzzy"})

RELATION_DUPLICATE = "duplicate"
RELATION_FORMAT_VARIANT = "format_variant"
RELATION_SAME_WORK = "same_work"

# Union-find takes the transitive closure, so a single wrong edge can chain two
# unrelated groups together.  A genuine duplicate cluster in this collection is
# small -- the same book scraped from a handful of sites.  Anything larger is far
# more likely to be a boilerplate leak than a book that exists twenty times, so
# it is flagged for review instead of being acted on.
MAX_AUTO_CLUSTER = 8


@dataclass(frozen=True)
class DuplicateEdge:
    left_id: int
    right_id: int
    method: str
    score: float
    evidence: dict = field(default_factory=dict, compare=False)

    def normalized(self) -> DuplicateEdge:
        """Order the endpoints so that an edge has one canonical form."""
        if self.left_id <= self.right_id:
            return self
        return DuplicateEdge(
            self.right_id, self.left_id, self.method, self.score, self.evidence
        )


def classify_relation(edge: DuplicateEdge, media_types: dict[int, str]) -> str:
    """Decide what an edge actually claims about its two documents.

    Content evidence between two files of the same format means one of them is
    redundant.  The same evidence between a PDF and an EPUB means the opposite:
    it is the strongest possible confirmation that the reflowable copy really is
    this book, and both are worth keeping.  Metadata evidence never rises above
    "probably the same work", because a second printing looks identical to a
    duplicate from the metadata alone.

    Text MinHash is deliberately weaker than the other content signals: it only
    sees a slice of the text layer, so a same-format text match is evidence of
    a shared work, never a licence to remove a file.  Only byte identity
    (``sha256``) and agreeing page renders (``page_image``) can make a
    ``duplicate``.
    """
    if edge.method in METADATA_METHODS:
        return RELATION_SAME_WORK
    if edge.method == "text_minhash":
        left = media_types.get(edge.left_id)
        right = media_types.get(edge.right_id)
        if not left or not right:
            return RELATION_SAME_WORK
        if left != right:
            return RELATION_FORMAT_VARIANT
        return RELATION_SAME_WORK
    left = media_types.get(edge.left_id)
    right = media_types.get(edge.right_id)
    if not left or not right:
        return RELATION_SAME_WORK
    if left and right and left != right:
        return RELATION_FORMAT_VARIANT
    return RELATION_DUPLICATE


@dataclass
class DuplicateCluster:
    document_ids: list[int]
    methods: set[str]
    relation: str
    status: str  # 'auto' when safe to act on, 'review' when a human must look
    keeper_id: int | None = None

    @property
    def size(self) -> int:
        return len(self.document_ids)

    @property
    def removable(self) -> bool:
        """Only redundant same-format copies are ever removal candidates."""
        return self.relation == RELATION_DUPLICATE and self.status == "auto"


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[max(left_root, right_root)] = min(left_root, right_root)

    def groups(self) -> dict[int, list[int]]:
        result: dict[int, list[int]] = defaultdict(list)
        for item in self._parent:
            result[self.find(item)].append(item)
        return {root: sorted(members) for root, members in result.items()}


def build_clusters(
    edges: list[DuplicateEdge],
    media_types: dict[int, str],
    *,
    relation: str = RELATION_DUPLICATE,
    max_auto_cluster: int = MAX_AUTO_CLUSTER,
) -> list[DuplicateCluster]:
    """Group documents joined by edges that make one kind of claim.

    Clustering is done per relation and never across relations: chaining a
    ``duplicate`` edge to a ``format_variant`` edge would drag an EPUB into a
    group of redundant PDFs and put it at risk of being scored out.
    """
    selected = [
        edge.normalized() for edge in edges
        if classify_relation(edge, media_types) == relation
    ]
    union = _UnionFind()
    methods: dict[int, set[str]] = defaultdict(set)
    for edge in selected:
        union.union(edge.left_id, edge.right_id)
    for edge in selected:
        methods[union.find(edge.left_id)].add(edge.method)

    clusters = [
        DuplicateCluster(
            document_ids=members,
            methods=methods[root],
            relation=relation,
            status="review" if len(members) > max_auto_cluster else "auto",
        )
        for root, members in union.groups().items()
        if len(members) > 1
    ]
    clusters.sort(key=lambda cluster: (-cluster.size, cluster.document_ids[0]))
    return clusters


@dataclass(frozen=True)
class DocumentQuality:
    """The facts that decide which copy of a book is worth keeping."""

    document_id: int
    media_type: str = "application/pdf"
    file_size: int = 0
    page_count: int | None = None
    has_text_layer: bool = False
    has_title: bool = False
    has_authors: bool = False
    catalogue_matched: bool = False
    source_template: str | None = None
    # Mean Tesseract word confidence over sampled pages, 0-100.  Measured only
    # for members of a duplicate cluster, because it is far too expensive to run
    # over the whole collection and is only ever needed to break a tie.
    ocr_confidence: float | None = None


# Sites differ in how much they damage what they redistribute.  Watermarked and
# banner-stamped scans are measurably worse copies of the same book, so where two
# sites carry it the cleaner source wins.  Values are small: provenance breaks
# ties, it does not outrank a file that is actually more legible.
_SOURCE_TRUST = {
    "gutenberg_bengali": 1.0,
    "bengaliebook": 0.8,
    "rarebooksociety": 0.6,
    "authordir": 0.4,
    "amarboi": 0.3,
    "banglabookshelf": 0.2,
    "banglabooks_in": 0.2,
    "amarbooks": 0.1,
    "granthagara": 0.0,  # brands every file with a full-page banner
}


def quality_score(
    document: DocumentQuality,
    *,
    max_page_count: int = 0,
    max_file_size: int = 0,
    page_counts_agree: bool = False,
) -> tuple[float, dict[str, float]]:
    """Score one copy, returning the total and every term that produced it.

    The terms are returned alongside the total so that a keeper decision can be
    audited rather than trusted -- the report writes them out per cluster.
    """
    terms: dict[str, float] = {}

    # Two scans of one book differ in exactly one way that matters: how well the
    # text comes off the page.  Where that has been measured it outweighs every
    # other term, because it decides whether the OCR pipeline produces a readable
    # book or a mess.  4.0 over a 0-1 range is deliberately larger than the text
    # layer bonus below, since between two scans neither has a text layer at all.
    if document.ocr_confidence is not None:
        terms["ocr_confidence"] = 4.0 * (document.ocr_confidence / 100.0)

    # A copy that already carries a text layer is usable today; a pure scan needs
    # the whole OCR pipeline before it can be read or searched.
    terms["text_layer"] = 3.0 if document.has_text_layer else 0.0

    # Truncated scans are a real failure mode in this collection -- a 21-page
    # excerpt sitting beside the 524-page book.
    if max_page_count and document.page_count:
        terms["completeness"] = 2.0 * (document.page_count / max_page_count)

    # File size is a proxy for scan resolution, but only once page counts match;
    # otherwise it just measures which file is longer, which "completeness"
    # already covers, and would double-count it.
    if page_counts_agree and max_file_size and document.file_size:
        terms["resolution"] = 1.0 * (document.file_size / max_file_size)

    terms["title"] = 1.0 if document.has_title else 0.0
    terms["authors"] = 1.0 if document.has_authors else 0.0
    terms["catalogue_match"] = 1.5 if document.catalogue_matched else 0.0
    terms["source_trust"] = _SOURCE_TRUST.get(document.source_template or "", 0.0)

    return sum(terms.values()), terms


def choose_keeper(
    documents: list[DocumentQuality],
) -> tuple[int, dict[int, tuple[float, dict[str, float]]]]:
    """Pick the copy to keep, and return every candidate's scorecard."""
    if not documents:
        raise ValueError("cannot choose a keeper from an empty cluster")

    page_counts = [doc.page_count for doc in documents if doc.page_count]
    max_pages = max(page_counts) if page_counts else 0
    min_pages = min(page_counts) if page_counts else 0
    # "Agree" uses the same tolerance the visual pipeline blocks on, so the two
    # halves of the system share one definition of "the same length".
    agree = bool(max_pages) and (max_pages - min_pages) <= max(1, int(max_pages * 0.02))
    max_size = max((doc.file_size for doc in documents), default=0)

    scores = {
        doc.document_id: quality_score(
            doc,
            max_page_count=max_pages,
            max_file_size=max_size,
            page_counts_agree=agree,
        )
        for doc in documents
    }
    # Ties break on the lowest id: the earliest-discovered copy, which keeps the
    # decision stable across reruns.
    keeper = max(scores, key=lambda doc_id: (scores[doc_id][0], -doc_id))
    return keeper, scores


def assign_keepers(
    clusters: list[DuplicateCluster],
    qualities: dict[int, DocumentQuality],
) -> dict[int, dict[int, tuple[float, dict[str, float]]]]:
    """Fill in ``keeper_id`` for each cluster; return the scorecards by cluster.

    Clusters that are not removal candidates still get a keeper: there it means
    the copy the catalogue should present as primary, not a copy that outranks
    the others.
    """
    scorecards: dict[int, dict[int, tuple[float, dict[str, float]]]] = {}
    for index, cluster in enumerate(clusters):
        members = [
            qualities.get(doc_id, DocumentQuality(doc_id))
            for doc_id in cluster.document_ids
        ]
        keeper, scores = choose_keeper(members)
        cluster.keeper_id = keeper
        scorecards[index] = scores
    return scorecards


def summarize(clusters: list[DuplicateCluster]) -> dict[str, int]:
    """Headline counts, with removable copies reported separately.

    ``removable_copies`` is the only number that implies deleting anything; the
    format-variant and same-work totals describe links, not redundancy.
    """
    by_relation: dict[str, list[DuplicateCluster]] = defaultdict(list)
    for cluster in clusters:
        by_relation[cluster.relation].append(cluster)
    return {
        "clusters": len(clusters),
        "duplicate_clusters": len(by_relation[RELATION_DUPLICATE]),
        "format_variant_clusters": len(by_relation[RELATION_FORMAT_VARIANT]),
        "same_work_clusters": len(by_relation[RELATION_SAME_WORK]),
        "review_clusters": sum(1 for c in clusters if c.status == "review"),
        "documents_in_clusters": sum(cluster.size for cluster in clusters),
        "removable_copies": sum(
            cluster.size - 1 for cluster in clusters if cluster.removable
        ),
    }


def removal_candidates(clusters: list[DuplicateCluster]) -> list[int]:
    """Every document a confirmed dedupe would drop -- keepers always excluded."""
    return sorted(
        doc_id
        for cluster in clusters if cluster.removable
        for doc_id in cluster.document_ids
        if doc_id != cluster.keeper_id
    )


# --- Metadata signal ---------------------------------------------------------
#
# Titles and authors reach every document that has them, including the scanned
# PDFs no content signal can touch, and they cross formats freely.  What they
# cannot do is distinguish a duplicate file from a second printing, which is why
# everything below produces ``same_work`` edges only.

_MIN_TITLE_KEY = 8  # a two-syllable title on its own collides with hundreds of books


@dataclass(frozen=True)
class DocumentMetadata:
    document_id: int
    title_key: str = ""
    author_key: str = ""


def metadata_edges(
    documents: list[DocumentMetadata],
    *,
    fuzzy_threshold: float = 92.0,
) -> list[DuplicateEdge]:
    """Propose edges between documents describing the same book.

    Exact edges come from blocking on the normalized title key, which is free.
    Fuzzy edges are then computed only *within* a block sharing an author, so
    the fuzzy comparison never runs across the whole corpus -- 76k names against
    each other is 2.9 billion comparisons, one author's shelf is a few hundred.
    """
    from rapidfuzz import fuzz

    by_title: dict[str, list[int]] = defaultdict(list)
    for document in documents:
        if len(document.title_key) >= _MIN_TITLE_KEY:
            by_title[document.title_key].append(document.document_id)

    edges: list[DuplicateEdge] = []
    for title_key, members in by_title.items():
        # A "title" shared by dozens of files is a template artefact -- a site
        # stamp or a generic label -- not dozens of copies of one book.
        if len(members) < 2 or len(members) > 20:
            continue
        ordered = sorted(members)
        for position, left in enumerate(ordered):
            for right in ordered[position + 1:]:
                edges.append(DuplicateEdge(
                    left, right, "metadata_exact", 1.0, {"title_key": title_key},
                ))

    by_author: dict[str, list[DocumentMetadata]] = defaultdict(list)
    for document in documents:
        if document.author_key and len(document.title_key) >= _MIN_TITLE_KEY:
            by_author[document.author_key].append(document)

    seen = {(edge.left_id, edge.right_id) for edge in edges}
    for author_key, shelf in by_author.items():
        if len(shelf) < 2 or len(shelf) > 500:
            continue
        for position, left in enumerate(shelf):
            for right in shelf[position + 1:]:
                if left.title_key == right.title_key:
                    continue  # already covered by the exact pass
                pair = (min(left.document_id, right.document_id),
                        max(left.document_id, right.document_id))
                if pair in seen:
                    continue
                score = fuzz.ratio(left.title_key, right.title_key)
                if score >= fuzzy_threshold:
                    seen.add(pair)
                    edges.append(DuplicateEdge(
                        pair[0], pair[1], "metadata_fuzzy", score / 100.0,
                        {"author_key": author_key,
                         "titles": [left.title_key, right.title_key]},
                    ))
    return edges


# --- Persistence -------------------------------------------------------------


def _has_column(conn, table: str, column: str) -> bool:
    """Tolerate side databases created before an additive column existed."""
    try:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:
        return False


def load_document_facts(
    conn,
) -> tuple[dict[int, DocumentQuality], list[DocumentMetadata]]:
    """Read the per-document facts the keeper choice and metadata signal need.

    Metadata lives in ``metadata_json`` as written by ``build_document_metadata``;
    the keys are normalized here rather than at write time so that a change to
    the normalizer takes effect without a full re-extraction.
    """
    from .text_normalize import normalize_name, normalize_text

    qualities: dict[int, DocumentQuality] = {}
    metadata: list[DocumentMetadata] = []
    matched = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT document_id FROM catalogue_document_matches "
            "WHERE status = 'accepted'"
        )
    }
    for doc_id, media_type, file_size, blob in conn.execute(
        "SELECT id, media_type, file_size, metadata_json FROM catalogue_local_documents"
    ):
        try:
            payload = json.loads(blob or "{}")
        except ValueError:
            payload = {}
        title = (payload.get("title") or "").strip()
        authors = payload.get("authors") or []
        qualities[doc_id] = DocumentQuality(
            document_id=doc_id,
            media_type=media_type,
            file_size=file_size or 0,
            page_count=payload.get("page_count"),
            has_text_layer=bool(payload.get("has_text_layer")),
            has_title=bool(title),
            has_authors=bool(authors),
            catalogue_matched=doc_id in matched,
            source_template=payload.get("template"),
        )
        metadata.append(DocumentMetadata(
            document_id=doc_id,
            title_key=normalize_text(title),
            author_key=normalize_name(authors[0]) if authors else "",
        ))
    return qualities, metadata


def store_edges(conn, edges: list[DuplicateEdge], *, run_id: int | None = None) -> int:
    has_run = _has_column(conn, "catalogue_duplicate_edges", "run_id")
    rows = [
        (edge.left_id, edge.right_id, edge.method, edge.score,
         json.dumps(edge.evidence, ensure_ascii=False))
        for edge in (edge.normalized() for edge in edges)
    ]
    if has_run and run_id is not None:
        conn.executemany(
            """INSERT INTO catalogue_duplicate_edges
               (left_document_id, right_document_id, method, score, evidence_json, run_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(left_document_id, right_document_id, method)
               DO UPDATE SET score=excluded.score, evidence_json=excluded.evidence_json,
                             run_id=excluded.run_id""",
            [row + (run_id,) for row in rows],
        )
    else:
        conn.executemany(
            """INSERT INTO catalogue_duplicate_edges
               (left_document_id, right_document_id, method, score, evidence_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(left_document_id, right_document_id, method)
               DO UPDATE SET score=excluded.score, evidence_json=excluded.evidence_json""",
            rows,
        )
    return len(rows)


def store_clusters(
    conn,
    clusters: list[DuplicateCluster],
    scorecards: dict[int, dict[int, tuple[float, dict[str, float]]]],
    *,
    replace: bool = True,
    run_id: int | None = None,
) -> int:
    """Write clusters and members.

    ``replace`` clears previous *automatic* rows only: a cluster a human has
    marked confirmed or rejected is a decision, and a rerun must not silently
    discard it.
    """
    has_run = _has_column(conn, "catalogue_duplicate_clusters", "run_id")
    if replace:
        conn.execute(
            "DELETE FROM catalogue_duplicate_clusters "
            "WHERE status IN ('auto', 'review')"
        )
    for index, cluster in enumerate(clusters):
        if has_run:
            cursor = conn.execute(
                """INSERT INTO catalogue_duplicate_clusters
                   (relation, status, methods, size, keeper_document_id, run_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cluster.relation, cluster.status, ",".join(sorted(cluster.methods)),
                 cluster.size, cluster.keeper_id, run_id),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO catalogue_duplicate_clusters
                   (relation, status, methods, size, keeper_document_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (cluster.relation, cluster.status, ",".join(sorted(cluster.methods)),
                 cluster.size, cluster.keeper_id),
            )
        cluster_id = cursor.lastrowid
        scores = scorecards.get(index, {})
        conn.executemany(
            """INSERT INTO catalogue_duplicate_members
               (cluster_id, document_id, is_keeper, score, scorecard_json)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (cluster_id, doc_id, 1 if doc_id == cluster.keeper_id else 0,
                 scores.get(doc_id, (0.0, {}))[0],
                 json.dumps(scores.get(doc_id, (0.0, {}))[1], ensure_ascii=False))
                for doc_id in cluster.document_ids
            ],
        )
    return len(clusters)


# --- Direct-keeper safety ----------------------------------------------------
#
# Clustering takes the transitive closure of the edges, so a chain A-B, B-C
# puts C in A's cluster even though nothing ever compared C with A directly.
# One wrong edge then makes an unrelated file removable.  Removal therefore
# requires *direct* duplicate evidence between the candidate and the keeper:
# a ``sha256`` or ``page_image`` edge joining exactly those two documents.


def duplicate_adjacency(
    edges: list[DuplicateEdge], media_types: dict[int, str]
) -> set[tuple[int, int]]:
    """Pairs joined by a directly removal-grade (``duplicate``) edge."""
    adjacent: set[tuple[int, int]] = set()
    for edge in edges:
        if classify_relation(edge, media_types) != RELATION_DUPLICATE:
            continue
        left, right = (
            (edge.left_id, edge.right_id)
            if edge.left_id <= edge.right_id
            else (edge.right_id, edge.left_id)
        )
        adjacent.add((left, right))
    return adjacent


def safe_removal_candidates(
    clusters: list[DuplicateCluster],
    edges: list[DuplicateEdge],
    media_types: dict[int, str],
) -> list[int]:
    """Removable documents with direct duplicate evidence to their keeper.

    A cluster member that reaches the keeper only through a third document --
    or only through text/metadata edges -- stays out of this list even when
    the cluster itself is ``auto``.  Keepers are never listed.
    """
    adjacent = duplicate_adjacency(edges, media_types)
    removable: list[int] = []
    for cluster in clusters:
        if not cluster.removable or cluster.keeper_id is None:
            continue
        for doc_id in cluster.document_ids:
            if doc_id == cluster.keeper_id:
                continue
            pair = (
                (doc_id, cluster.keeper_id)
                if doc_id <= cluster.keeper_id
                else (cluster.keeper_id, doc_id)
            )
            if pair in adjacent:
                removable.append(doc_id)
    return sorted(removable)


# --- Whole-corpus workflow ---------------------------------------------------
#
# Five explicit phases, each a CLI subcommand.  The first three only read the
# collection and write the *side* database; only ``apply`` moves files, and
# only on the basis of a specific report it is handed, into a quarantine
# directory it is told -- never by deleting anything.  ``rollback`` reverses
# exactly the moves one manifest records.
#
# The side database is any SQLite file the caller names (a copy of the
# catalogue, or a fresh file initialized with the catalogue schema).  The live
# catalogue database is left alone unless the caller explicitly passes its
# path as ``--db``.

DEDUPE_TOOL_VERSION = "1.0"

SUPPORTED_EXTENSIONS = {".pdf", ".epub"}

_OK = "ok"
_UNREADABLE = "unreadable"
_UNSUPPORTED = "unsupported"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_ns_now() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000_000_000)


def sha256_of(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it into memory.  Raises ``OSError``."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_type_for(path: str | Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def ensure_workflow_tables(conn) -> None:
    """Create the run-provenance tables on a side database that lacks them.

    Fresh databases from :func:`initialize_database` already have these (they
    are part of the dedupe DDL); this keeps older side databases usable.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dedupe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus_root TEXT NOT NULL,
            tool_version TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running', 'completed', 'failed')),
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dedupe_path_identities (
            run_id INTEGER NOT NULL REFERENCES dedupe_runs(id) ON DELETE CASCADE,
            source_path TEXT NOT NULL,
            sha256 TEXT,
            file_size INTEGER,
            mtime_ns INTEGER,
            media_type TEXT,
            status TEXT NOT NULL
                CHECK(status IN ('ok', 'unreadable', 'unsupported')),
            error TEXT,
            PRIMARY KEY (run_id, source_path)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dedupe_run_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES dedupe_runs(id) ON DELETE CASCADE,
            source_path TEXT NOT NULL,
            stage TEXT NOT NULL,
            error TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dedupe_identities_run "
        "ON dedupe_path_identities(run_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dedupe_failures_run "
        "ON dedupe_run_failures(run_id)"
    )
    conn.commit()


def start_run(conn, corpus_root: str | Path, params: dict | None = None) -> int:
    ensure_workflow_tables(conn)
    cursor = conn.execute(
        "INSERT INTO dedupe_runs (corpus_root, tool_version, params_json) "
        "VALUES (?, ?, ?)",
        (str(corpus_root), DEDUPE_TOOL_VERSION,
         json.dumps(params or {}, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(conn, run_id: int, status: str = "completed") -> None:
    if status not in {"completed", "failed"}:
        raise ValueError("status must be completed or failed")
    conn.execute(
        "UPDATE dedupe_runs SET status=?, finished_at=datetime('now') WHERE id=?",
        (status, run_id),
    )
    conn.commit()


def record_failure(conn, run_id: int, source_path: str, stage: str, error: str) -> None:
    conn.execute(
        "INSERT INTO dedupe_run_failures (run_id, source_path, stage, error) "
        "VALUES (?, ?, ?, ?)",
        (run_id, source_path, stage, error),
    )


def _record_identity(
    conn,
    run_id: int,
    source_path: str,
    *,
    status: str,
    sha256: str | None = None,
    file_size: int | None = None,
    mtime_ns: int | None = None,
    media_type: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO dedupe_path_identities
           (run_id, source_path, sha256, file_size, mtime_ns, media_type,
            status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id, source_path) DO UPDATE SET
           sha256=excluded.sha256, file_size=excluded.file_size,
           mtime_ns=excluded.mtime_ns, media_type=excluded.media_type,
           status=excluded.status, error=excluded.error""",
        (run_id, source_path, sha256, file_size, mtime_ns, media_type,
         status, error),
    )


def inventory_corpus(
    conn,
    corpus_root: str | Path,
    *,
    extensions: tuple[str, ...] = ("pdf", "epub"),
    params: dict | None = None,
) -> int:
    """Walk the corpus, hash every supported file, snapshot path identities.

    Returns the new run id.  One unreadable file never aborts the run: it is
    recorded with status ``unreadable`` plus a failure row.  Supported files
    are registered as local documents (keyed by SHA-256, so repeated paths to
    identical bytes become aliases of one document) and snapshotted per path.
    """
    from .foundation import CatalogueFoundation
    from types import SimpleNamespace

    # Store lexical absolute paths, but never follow symlinks while walking the
    # corpus.  A symlink can point outside /data and must not make that target
    # part of the dedupe universe.
    base = Path(corpus_root).resolve()
    if not base.is_dir():
        raise NotADirectoryError(base)
    wanted = {"." + ext.lstrip(".").lower() for ext in extensions}
    run_id = start_run(conn, base, params or {"extensions": sorted(wanted)})
    foundation = CatalogueFoundation(SimpleNamespace(conn=conn))
    try:
        entries = sorted(base.rglob("*"), key=lambda path: str(path))
    except OSError as exc:
        record_failure(conn, run_id, str(base), "inventory", str(exc))
        finish_run(conn, run_id, "failed")
        raise
    paths: list[Path] = []
    for entry in entries:
        if entry.is_symlink():
            # A dangling symlink is a malformed collection entry, not an
            # absence: record every symlink and keep going without following
            # even valid links to files outside the corpus.
            _record_identity(conn, run_id, str(entry), status=_UNREADABLE,
                             error="symlink not followed",
                             media_type=media_type_for(entry))
            record_failure(conn, run_id, str(entry), "inventory",
                           "symlink not followed")
        elif entry.is_file():
            paths.append(entry)
    for path in paths:
        source = str(path)
        if path.suffix.lower() not in wanted:
            try:
                stat = path.stat()
                size, mtime = stat.st_size, stat.st_mtime_ns
            except OSError:
                stat, size, mtime = None, None, None
            _record_identity(conn, run_id, source, status=_UNSUPPORTED,
                             file_size=size, mtime_ns=mtime,
                             media_type=media_type_for(path))
            try:
                foundation.record_local_inventory(
                    path, root=base, status="unsupported", stat_result=stat)
            except (OSError, ValueError):
                pass
            continue
        try:
            stat = path.stat()
            digest = sha256_of(path)
        except OSError as exc:
            _record_identity(conn, run_id, source, status=_UNREADABLE,
                             error=str(exc), media_type=media_type_for(path))
            record_failure(conn, run_id, source, "inventory", str(exc))
            try:
                foundation.record_local_inventory(
                    path, root=base, status="unreadable", error=str(exc))
            except (OSError, ValueError):
                pass
            continue
        try:
            document = foundation.upsert_local_document(
                path, sha256=digest, metadata={})
            foundation.record_local_inventory(
                path, root=base, status="imported", document_id=document.id,
                sha256=digest, stat_result=stat)
        except (OSError, ValueError) as exc:
            _record_identity(conn, run_id, source, status=_UNREADABLE,
                             file_size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                             media_type=media_type_for(path), error=str(exc))
            record_failure(conn, run_id, source, "inventory", str(exc))
            continue
        _record_identity(conn, run_id, source, status=_OK, sha256=digest,
                         file_size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                         media_type=media_type_for(path))
    conn.commit()
    finish_run(conn, run_id, "completed")
    return run_id


def _run_row(conn, run_id: int) -> dict:
    row = conn.execute(
        "SELECT id, corpus_root, tool_version, params_json, status, "
        "started_at, finished_at FROM dedupe_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown dedupe run {run_id}")
    return {
        "id": row[0], "corpus_root": row[1], "tool_version": row[2],
        "params": json.loads(row[3] or "{}"), "status": row[4],
        "started_at": row[5], "finished_at": row[6],
    }


def _run_identities(conn, run_id: int, *, status: str | None = None) -> list[dict]:
    query = (
        "SELECT source_path, sha256, file_size, mtime_ns, media_type, status,"
        " error FROM dedupe_path_identities WHERE run_id=?"
    )
    args: list[object] = [run_id]
    if status is not None:
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY source_path"
    return [
        {"path": row[0], "sha256": row[1], "file_size": row[2],
         "mtime_ns": row[3], "media_type": row[4], "status": row[5],
         "error": row[6]}
        for row in conn.execute(query, args).fetchall()
    ]


def _document_ids_by_sha(conn) -> dict[str, int]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT sha256, id FROM catalogue_local_documents"
        ).fetchall()
    }


def _stored_page_fingerprints(conn, document_id: int):
    """Cached fingerprints, or ``None`` when absent or version-stale."""
    from .page_fingerprint import (FINGERPRINT_ALGO_VERSION, PageFingerprint,
                                   from_signed64)

    if not _has_column(conn, "catalogue_page_fingerprints", "algo_version"):
        return None
    rows = conn.execute(
        "SELECT page_index, dhash, informative, page_count, algo_version, stddev"
        " FROM catalogue_page_fingerprints WHERE document_id=? ORDER BY page_index",
        (document_id,),
    ).fetchall()
    if not rows:
        return None
    if any(row[4] != FINGERPRINT_ALGO_VERSION for row in rows):
        return None
    page_count = rows[0][3]
    return (
        [PageFingerprint(row[0], from_signed64(row[1]), row[5] or 0.0,
                         bool(row[2])) for row in rows],
        page_count,
    )


def _store_page_fingerprints(conn, document_id: int, fingerprints, page_count: int) -> None:
    from .page_fingerprint import FINGERPRINT_ALGO_VERSION, to_signed64

    conn.execute(
        "DELETE FROM catalogue_page_fingerprints WHERE document_id=?",
        (document_id,),
    )
    has_version = _has_column(conn, "catalogue_page_fingerprints", "algo_version")
    has_stddev = _has_column(conn, "catalogue_page_fingerprints", "stddev")
    for page in fingerprints:
        if has_version and has_stddev:
            conn.execute(
                "INSERT INTO catalogue_page_fingerprints "
                "(document_id, page_index, dhash, informative, page_count,"
                " algo_version, stddev) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, page.page_index, to_signed64(page.dhash),
                 1 if page.informative else 0, page_count,
                 FINGERPRINT_ALGO_VERSION, page.stddev),
            )
        else:
            conn.execute(
                "INSERT INTO catalogue_page_fingerprints "
                "(document_id, page_index, dhash, informative, page_count)"
                " VALUES (?, ?, ?, ?, ?)",
                (document_id, page.page_index, to_signed64(page.dhash),
                 1 if page.informative else 0, page_count),
            )


def _stored_text_signature(conn, document_id: int):
    """Cached text signature, or ``None`` when absent or version-stale."""
    from .text_signature import TEXT_SIGNATURE_ALGO_VERSION, TextSignature

    if not _has_column(conn, "catalogue_text_signatures", "algo_version"):
        return None
    row = conn.execute(
        "SELECT signature_json, shingle_count, algo_version"
        " FROM catalogue_text_signatures WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if row is None or row[2] != TEXT_SIGNATURE_ALGO_VERSION:
        return None
    return TextSignature(document_id, tuple(json.loads(row[0])), row[1])


def _store_text_signature(conn, signature) -> None:
    from .text_signature import TEXT_SIGNATURE_ALGO_VERSION

    if _has_column(conn, "catalogue_text_signatures", "algo_version"):
        conn.execute(
            "INSERT INTO catalogue_text_signatures "
            "(document_id, signature_json, shingle_count, algo_version)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(document_id) DO UPDATE SET"
            " signature_json=excluded.signature_json,"
            " shingle_count=excluded.shingle_count,"
            " algo_version=excluded.algo_version,"
            " computed_at=datetime('now')",
            (signature.document_id, json.dumps(list(signature.signature)),
             signature.shingle_count, TEXT_SIGNATURE_ALGO_VERSION),
        )
    else:
        conn.execute(
            "INSERT INTO catalogue_text_signatures "
            "(document_id, signature_json, shingle_count)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(document_id) DO UPDATE SET"
            " signature_json=excluded.signature_json,"
            " shingle_count=excluded.shingle_count,"
            " computed_at=datetime('now')",
            (signature.document_id, json.dumps(list(signature.signature)),
             signature.shingle_count),
        )


def _merge_document_metadata(conn, document_id: int, extra: dict) -> None:
    row = conn.execute(
        "SELECT metadata_json FROM catalogue_local_documents WHERE id=?",
        (document_id,),
    ).fetchone()
    try:
        payload = json.loads((row[0] if row else None) or "{}")
    except ValueError:
        payload = {}
    payload.update(extra)
    conn.execute(
        "UPDATE catalogue_local_documents SET metadata_json=?,"
        " updated_at=datetime('now') WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), document_id),
    )


def sha256_edges(
    documents: list[tuple[int, str]],
) -> list[DuplicateEdge]:
    """One edge per pair of documents sharing identical bytes."""
    by_sha: dict[str, list[int]] = defaultdict(list)
    for document_id, sha in documents:
        by_sha[sha].append(document_id)
    edges: list[DuplicateEdge] = []
    for sha, members in by_sha.items():
        if len(members) < 2:
            continue
        ordered = sorted(members)
        for position, left in enumerate(ordered):
            for right in ordered[position + 1:]:
                edges.append(DuplicateEdge(
                    left, right, "sha256", 1.0, {"sha256": sha}))
    return edges


def _alias_duplicate_groups(
    conn, run_id: int, *, record_failures: bool = False,
) -> list[dict]:
    """Path groups sharing byte-identical content.

    Local documents are keyed by SHA-256, so two paths with identical bytes
    are *aliases* of one document row and can never meet as an edge pair.
    They are nonetheless the clearest duplicates in the corpus -- identical
    bytes in the same format -- and are handled here as path-level groups:
    the earliest sorted path keeps, the rest are removal candidates with
    direct byte-identity evidence to the keeper.  Groups whose identical
    bytes wear different media types are refused, not guessed about.
    """
    identities = _run_identities(conn, run_id, status=_OK)
    documents = _document_ids_by_sha(conn)
    by_sha: dict[str, list[dict]] = defaultdict(list)
    for identity in identities:
        if identity["sha256"]:
            by_sha[identity["sha256"]].append(identity)
    groups: list[dict] = []
    for sha in sorted(by_sha):
        items = by_sha[sha]
        if len(items) < 2:
            continue
        medias = {item["media_type"] or "application/octet-stream"
                  for item in items}
        if len(medias) != 1:
            if record_failures:
                record_failure(
                    conn, run_id, items[0]["path"], "analyze",
                    "identical bytes under mixed media types: "
                    + ",".join(sorted(medias)))
            continue
        paths = sorted(item["path"] for item in items)
        groups.append({
            "sha256": sha,
            "document_id": documents.get(sha),
            "media_type": next(iter(medias)),
            "keeper_path": paths[0],
            "loser_paths": paths[1:],
        })
    return groups


def analyze_run(
    conn,
    run_id: int,
    *,
    page_counter=None,
    fingerprinter=None,
    text_signer=None,
    with_metadata: bool = True,
    max_auto_cluster: int = MAX_AUTO_CLUSTER,
) -> dict:
    """Match one inventoried run and persist edges, clusters and keepers.

    ``page_counter`` maps a path to a page count, ``fingerprinter`` maps
    ``(path, page_count)`` to page fingerprints, and ``text_signer`` maps
    ``(document_id, path, media_type)`` to a text signature or ``None``.  The
    defaults shell out to poppler; tests inject fakes.  Cached fingerprints
    and signatures are reused only when the content SHA-256 and the
    algorithm version both match -- a changed file hashes differently and
    therefore misses the cache, and a bumped version invalidates it.
    """
    run = _run_row(conn, run_id)
    identities = _run_identities(conn, run_id, status=_OK)
    documents = _document_ids_by_sha(conn)
    media_types: dict[int, str] = {}
    doc_paths: dict[int, str] = {}
    doc_sizes: dict[int, int] = {}
    for identity in identities:
        document_id = documents.get(identity["sha256"] or "")
        if document_id is None:
            record_failure(conn, run_id, identity["path"], "analyze",
                           "no local document for inventoried sha256")
            continue
        media_types[document_id] = identity["media_type"] or "application/pdf"
        doc_paths.setdefault(document_id, identity["path"])
        doc_sizes[document_id] = identity["file_size"] or 0

    if page_counter is None:
        from .page_fingerprint import page_count as page_counter
    if fingerprinter is None:
        from .page_fingerprint import fingerprint_document as _fp

        def fingerprinter(path, page_count):  # type: ignore[misc]
            return _fp(path, page_count)
    if text_signer is None:
        def text_signer(document_id, path, media_type):  # type: ignore[misc]
            from .text_signature import signature_document

            try:
                signature = signature_document(document_id, path, media_type)
            except Exception:
                return None
            return signature if signature.usable else None

    from .page_fingerprint import find_duplicate_documents
    from .text_signature import find_text_duplicates

    page_fingerprints: dict[int, list] = {}
    page_counts: dict[int, int] = {}
    for document_id, path in doc_paths.items():
        cached = _stored_page_fingerprints(conn, document_id)
        if cached is not None:
            fingerprints, stored_count = cached
            page_fingerprints[document_id] = fingerprints
            if stored_count:
                page_counts[document_id] = stored_count
            continue
        try:
            count = int(page_counter(path) or 0)
        except Exception as exc:
            record_failure(conn, run_id, path, "page_count", str(exc))
            continue
        page_counts[document_id] = count
        if count <= 0:
            continue
        try:
            fingerprints = list(fingerprinter(path, count) or [])
        except Exception as exc:
            record_failure(conn, run_id, path, "fingerprint", str(exc))
            continue
        if count > 0 and not fingerprints:
            record_failure(conn, run_id, path, "fingerprint",
                           "no pages rendered")
            continue
        _store_page_fingerprints(conn, document_id, fingerprints, count)
        _merge_document_metadata(conn, document_id, {"page_count": count})
        page_fingerprints[document_id] = fingerprints

    text_signatures: dict[int, object] = {}
    for document_id, path in doc_paths.items():
        cached = _stored_text_signature(conn, document_id)
        if cached is not None:
            if cached.usable:
                text_signatures[document_id] = cached
            continue
        try:
            signature = text_signer(
                document_id, path, media_types.get(document_id, ""))
        except Exception as exc:
            record_failure(conn, run_id, path, "text_signature", str(exc))
            continue
        if signature is None:
            continue
        _store_text_signature(conn, signature)
        if signature.usable:
            text_signatures[document_id] = signature

    edges: list[DuplicateEdge] = sha256_edges(
        [(doc_id, sha) for doc_id, sha in
         ((doc_id, _sha_for_document(conn, doc_id)) for doc_id in doc_paths)
         if sha]
    )
    for pair in find_duplicate_documents(page_fingerprints, page_counts):
        edges.append(DuplicateEdge(
            pair.left_id, pair.right_id, "page_image",
            float(pair.pages_agreeing),
            {"pages_agreeing": pair.pages_agreeing,
             "page_count_delta": pair.page_count_delta}))
    for left, right, score in find_text_duplicates(text_signatures):  # type: ignore[arg-type]
        edges.append(DuplicateEdge(
            left, right, "text_minhash", float(score),
            {"jaccard": float(score)}))
    if with_metadata:
        try:
            _, metadata = load_document_facts(conn)
            metadata = [item for item in metadata if item.document_id in doc_paths]
            edges.extend(metadata_edges(metadata))
        except Exception as exc:
            record_failure(conn, run_id, run["corpus_root"], "metadata",
                           str(exc))

    store_edges(conn, edges, run_id=run_id)

    clusters: list[DuplicateCluster] = []
    for relation in (RELATION_DUPLICATE, RELATION_FORMAT_VARIANT,
                     RELATION_SAME_WORK):
        clusters.extend(build_clusters(
            edges, media_types, relation=relation,
            max_auto_cluster=max_auto_cluster))
    qualities: dict[int, DocumentQuality] = {}
    for document_id in doc_paths:
        try:
            row = conn.execute(
                "SELECT file_size, metadata_json FROM catalogue_local_documents"
                " WHERE id=?", (document_id,),
            ).fetchone()
            payload = json.loads((row[1] if row else None) or "{}")
        except ValueError:
            payload, row = {}, (None, None)
        qualities[document_id] = DocumentQuality(
            document_id=document_id,
            media_type=media_types.get(document_id, "application/pdf"),
            file_size=(row[0] if row else None) or 0,
            page_count=payload.get("page_count", page_counts.get(document_id)),
            has_text_layer=document_id in text_signatures,
            has_title=bool((payload.get("title") or "").strip()),
            has_authors=bool(payload.get("authors")),
            source_template=payload.get("template"),
        )
    scorecards = assign_keepers(clusters, qualities)
    store_clusters(conn, clusters, scorecards, run_id=run_id)
    # Byte-identical aliases never appear as edges (one document row), so
    # they are counted here from the path identities directly.
    alias_groups = _alias_duplicate_groups(conn, run_id, record_failures=True)
    conn.commit()
    finish_run(conn, run_id, "completed")
    summary = summarize(clusters)
    summary["clusters"] += len(alias_groups)
    summary["duplicate_clusters"] += len(alias_groups)
    summary["documents_in_clusters"] += sum(
        1 + len(group["loser_paths"]) for group in alias_groups)
    summary["removable_copies"] += sum(
        len(group["loser_paths"]) for group in alias_groups)
    summary["safe_removable_copies"] = (
        len(safe_removal_candidates(clusters, edges, media_types))
        + sum(len(group["loser_paths"]) for group in alias_groups))
    summary["run_id"] = run_id
    return summary


def _sha_for_document(conn, document_id: int) -> str | None:
    row = conn.execute(
        "SELECT sha256 FROM catalogue_local_documents WHERE id=?",
        (document_id,),
    ).fetchone()
    return row[0] if row else None


def _clusters_for_run(conn, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, relation, status, methods, keeper_document_id"
        " FROM catalogue_duplicate_clusters WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    clusters = []
    for row in rows:
        members = conn.execute(
            "SELECT document_id, is_keeper, score, scorecard_json"
            " FROM catalogue_duplicate_members WHERE cluster_id=? ORDER BY document_id",
            (row[0],),
        ).fetchall()
        clusters.append({
            "id": row[0], "relation": row[1], "status": row[2],
            "methods": [m for m in (row[3] or "").split(",") if m],
            "keeper_document_id": row[4],
            "members": [
                {"document_id": m[0], "is_keeper": bool(m[1]),
                 "score": m[2], "scorecard": json.loads(m[3] or "{}")}
                for m in members
            ],
        })
    return clusters


def _edges_for_run(conn, run_id: int) -> list[DuplicateEdge]:
    query = (
        "SELECT left_document_id, right_document_id, method, score, evidence_json"
        " FROM catalogue_duplicate_edges"
    )
    args: list[object] = []
    if _has_column(conn, "catalogue_duplicate_edges", "run_id"):
        query += " WHERE run_id=?"
        args.append(run_id)
    else:
        # A legacy edge table has no provenance and cannot safely participate
        # in a run-specific report.
        return []
    return [
        DuplicateEdge(row[0], row[1], row[2], row[3],
                      json.loads(row[4] or "{}"))
        for row in conn.execute(query, args).fetchall()
    ]


def build_report(conn, run_id: int) -> dict:
    """Assemble the reconciled JSON-serializable report for one run."""
    run = _run_row(conn, run_id)
    identities = _run_identities(conn, run_id)
    failures = [
        {"path": row[0], "stage": row[1], "error": row[2]}
        for row in conn.execute(
            "SELECT source_path, stage, error FROM dedupe_run_failures"
            " WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall()
    ]
    ok = [identity for identity in identities if identity["status"] == _OK]
    by_status: dict[str, int] = defaultdict(int)
    for identity in identities:
        by_status[identity["status"]] += 1
    unique_contents = len({identity["sha256"] for identity in ok
                           if identity["sha256"]})

    documents = _document_ids_by_sha(conn)
    media_types: dict[int, str] = {}
    primary_path: dict[int, str] = {}
    for identity in ok:
        document_id = documents.get(identity["sha256"] or "")
        if document_id is None:
            continue
        media_types[document_id] = identity["media_type"] or "application/pdf"
        if document_id not in primary_path:
            primary_path[document_id] = identity["path"]

    clusters = _clusters_for_run(conn, run_id)
    edges = _edges_for_run(conn, run_id)
    adjacent = duplicate_adjacency(edges, media_types)

    removable: list[dict] = []
    for cluster in clusters:
        safe_for_cluster = []
        if cluster["relation"] == RELATION_DUPLICATE and cluster["status"] == "auto":
            keeper = cluster["keeper_document_id"]
            for member in cluster["members"]:
                doc_id = member["document_id"]
                if doc_id == keeper:
                    continue
                pair = ((doc_id, keeper) if doc_id <= keeper
                        else (keeper, doc_id))
                if pair in adjacent:
                    safe_for_cluster.append(doc_id)
        cluster["removable_members"] = sorted(safe_for_cluster)
        keeper_path = primary_path.get(cluster["keeper_document_id"] or -1)
        for doc_id in safe_for_cluster:
            row = conn.execute(
                "SELECT sha256, file_size FROM catalogue_local_documents WHERE id=?",
                (doc_id,),
            ).fetchone()
            identity = next(
                (item for item in ok
                 if documents.get(item["sha256"] or "") == doc_id),
                None,
            )
            removable.append({
                "path": primary_path.get(doc_id, ""),
                "sha256": row[0] if row else "",
                "file_size": (row[1] if row else 0) or 0,
                "mtime_ns": identity["mtime_ns"] if identity else None,
                "cluster_id": cluster["id"],
                "keeper_document_id": cluster["keeper_document_id"],
                "keeper_path": keeper_path or "",
                "methods": cluster["methods"],
            })
    removable.sort(key=lambda item: item["path"])

    # Byte-identical aliases: same bytes, same format, several paths.  The
    # earliest sorted path keeps; every other path is removable with direct
    # byte-identity evidence to the keeper.  These groups are derived
    # deterministically from the run's path identities, so rebuilding the
    # report reproduces them exactly.
    by_path = {identity["path"]: identity for identity in ok}
    for group in _alias_duplicate_groups(conn, run_id):
        document_id = group["document_id"]
        if document_id is None:
            continue
        row = conn.execute(
            "SELECT file_size FROM catalogue_local_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        size = (row[0] if row else 0) or 0
        alias_id = f"alias-{group['sha256'][:12]}"
        members = []
        for position, path in enumerate(
                [group["keeper_path"], *group["loser_paths"]]):
            members.append({
                "document_id": document_id, "path": path,
                "is_keeper": position == 0,
                "score": 1.0 if position == 0 else 0.0,
                "scorecard": {"basis": "byte-identical alias"},
            })
        clusters.append({
            "id": alias_id, "relation": RELATION_DUPLICATE, "status": "auto",
            "methods": ["sha256"],
            "keeper_document_id": document_id,
            "keeper_path": group["keeper_path"],
            "members": members,
            "removable_members": [],
            "removable_paths": list(group["loser_paths"]),
        })
        for loser in group["loser_paths"]:
            identity = by_path.get(loser, {})
            removable.append({
                "path": loser,
                "sha256": group["sha256"],
                "file_size": size,
                "mtime_ns": identity.get("mtime_ns"),
                "cluster_id": alias_id,
                "keeper_document_id": document_id,
                "keeper_path": group["keeper_path"],
                "methods": ["sha256"],
            })
    removable.sort(key=lambda item: item["path"])
    estimated_bytes = sum(item["file_size"] for item in removable)

    pseudo = [
        DuplicateCluster(
            document_ids=[m["document_id"] for m in cluster["members"]],
            methods=set(cluster["methods"]), relation=cluster["relation"],
            status=cluster["status"],
            keeper_id=cluster["keeper_document_id"],
        )
        for cluster in clusters
        if not str(cluster["id"]).startswith("alias-")
    ]
    summary = summarize(pseudo)
    alias_clusters = [c for c in clusters
                      if str(c["id"]).startswith("alias-")]
    summary["clusters"] += len(alias_clusters)
    summary["duplicate_clusters"] += len(alias_clusters)
    summary["documents_in_clusters"] += sum(
        len(c["members"]) for c in alias_clusters)
    summary["removable_copies"] += sum(
        len(c.get("removable_paths", [])) for c in alias_clusters)
    summary["safe_removable_copies"] = len(removable)

    return {
        "dedupe_tool_version": DEDUPE_TOOL_VERSION,
        "run_id": run_id,
        "corpus_root": run["corpus_root"],
        "created_at": _now(),
        "inventory": {
            "discovered_paths": len(identities),
            "ok": by_status.get(_OK, 0),
            "unreadable": by_status.get(_UNREADABLE, 0),
            "unsupported": by_status.get(_UNSUPPORTED, 0),
            "unique_contents": unique_contents,
            "failed_total": len(failures),
        },
        "failures": failures,
        "clusters": clusters,
        "summary": summary,
        "removable": removable,
        "estimated_bytes_saved": estimated_bytes,
    }


def reconcile_report(report: dict) -> list[str]:
    """Check a report for internal contradictions; ``[]`` means clean."""
    problems: list[str] = []
    inventory = report.get("inventory", {})
    discovered = inventory.get("discovered_paths", 0)
    parts = inventory.get("ok", 0) + inventory.get("unreadable", 0) \
        + inventory.get("unsupported", 0)
    if discovered != parts:
        problems.append(
            f"discovered_paths {discovered} != ok+unreadable+unsupported {parts}")
    if len(report.get("failures", [])) != inventory.get("failed_total", 0):
        problems.append("failures list length != failed_total")
    removable = report.get("removable", [])
    if len(removable) != report.get("summary", {}).get(
            "safe_removable_copies", len(removable)):
        problems.append("removable list length != safe_removable_copies")
    if sum(item.get("file_size", 0) for item in removable) != report.get(
            "estimated_bytes_saved", 0):
        problems.append("estimated_bytes_saved != sum of removable file sizes")
    seen_paths: set[str] = set()
    for item in removable:
        if item.get("path") in seen_paths:
            problems.append(f"duplicate removable path {item.get('path')}")
        seen_paths.add(item.get("path"))
        if item.get("path") == item.get("keeper_path"):
            problems.append(f"keeper listed as removable: {item.get('path')}")
        if not item.get("sha256"):
            problems.append(f"removable entry without sha256: {item.get('path')}")
    cluster_members = sum(len(cluster.get("members", []))
                          for cluster in report.get("clusters", []))
    if cluster_members != report.get("summary", {}).get(
            "documents_in_clusters", cluster_members):
        problems.append("cluster membership total != documents_in_clusters")
    return problems


def write_report_files(report: dict, json_path: str | Path,
                       csv_path: str | Path) -> None:
    json_path = Path(json_path)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with open(csv_path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("path", "sha256", "file_size", "cluster_id",
                         "keeper_document_id", "keeper_path", "methods"))
        for item in report.get("removable", []):
            writer.writerow((
                item.get("path", ""), item.get("sha256", ""),
                item.get("file_size", 0), item.get("cluster_id", ""),
                item.get("keeper_document_id", ""),
                item.get("keeper_path", ""),
                ",".join(item.get("methods", [])),
            ))


def load_report_file(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as stream:
        report = json.load(stream)
    if not isinstance(report, dict) or "run_id" not in report \
            or "removable" not in report:
        raise ValueError(f"{path} is not a dedupe report")
    return report


def _canonical_sha(report: dict) -> str:
    return hashlib.sha256(
        json.dumps(report, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _collision_free_dest(quarantine_root: Path, sha256: str,
                         filename: str, *, create_dirs: bool = True) -> Path:
    """Destination that never overwrites: sharded by hash, suffixed on clash."""
    directory = quarantine_root / (sha256[:2] if sha256 else "unknown")
    if create_dirs:
        directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{sha256}_{filename}" if sha256 else directory / filename
    if not candidate.exists():
        return candidate
    try:
        if sha256 and sha256_of(candidate) == sha256:
            return candidate  # already quarantined; caller records a skip
    except OSError:
        pass
    stem, suffix = os.path.splitext(filename)
    counter = 1
    while counter <= 10000:
        alternative = directory / (
            f"{sha256}_{stem}-{counter}{suffix}" if sha256
            else f"{stem}-{counter}{suffix}")
        if not alternative.exists():
            return alternative
        counter += 1
    raise OSError("too many quarantine destination collisions")


def _write_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2,
                                sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _move_no_replace(src: Path, dst: Path, expected_sha256: str) -> None:
    """Move a regular file without ever replacing an existing destination.

    A hard-link/unlink pair is atomic with respect to destination creation on
    one filesystem.  The copy fallback is used for cross-device quarantine
    roots and reserves the destination with O_EXCL before writing it.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    source_fd = os.open(str(src), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_identity: tuple[int, int] | None = None
    try:
        def cleanup_destination() -> None:
            if destination_identity is None:
                return
            try:
                current_destination = os.lstat(dst)
                if ((current_destination.st_dev, current_destination.st_ino)
                        == destination_identity):
                    dst.unlink()
            except OSError:
                pass

        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError("source is not a regular file")

        def source_hash() -> str:
            os.lseek(source_fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            for chunk in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()

        if source_hash() != expected_sha256:
            raise OSError("source hash changed during move")
        try:
            # follow_symlinks=False ensures a path swap cannot make the
            # quarantine entry point at an external target.
            os.link(src, dst, follow_symlinks=False)
            destination_stat = os.lstat(dst)
            destination_identity = (destination_stat.st_dev,
                                    destination_stat.st_ino)
            dest_fd = os.open(str(dst), os.O_RDONLY |
                              getattr(os, "O_NOFOLLOW", 0))
            try:
                dest_stat = os.fstat(dest_fd)
                if (dest_stat.st_dev, dest_stat.st_ino) != (source_stat.st_dev,
                                                             source_stat.st_ino):
                    raise OSError("source changed during move")
            finally:
                os.close(dest_fd)
        except FileExistsError:
            raise
        except OSError:
            # Cross-device quarantine: reserve the destination with O_EXCL and
            # copy from the already-open, no-follow source descriptor.
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o644)
            destination_stat = os.fstat(fd)
            destination_identity = (destination_stat.st_dev,
                                    destination_stat.st_ino)
            try:
                os.lseek(source_fd, 0, os.SEEK_SET)
                with os.fdopen(fd, "wb") as target:
                    fd = -1
                    for chunk in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
            if sha256_of(dst) != expected_sha256:
                cleanup_destination()
                raise OSError("quarantine copy hash mismatch")
        # A hard link shares the inode with the source. Rehash immediately
        # before unlinking so an in-place writer cannot alter verified bytes
        # between the initial hash and the move.
        if source_hash() != expected_sha256:
            cleanup_destination()
            raise OSError("source changed during move")
        # Do not unlink a replacement path: the inode must still be the one
        # opened and hashed above.
        current_stat = os.stat(src, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != (source_stat.st_dev,
                                                            source_stat.st_ino):
            cleanup_destination()
            raise OSError("source changed before unlink")
        src.unlink()
    except Exception:
        # A failed hard-link leaves a destination behind; remove only a file
        # whose content matches the expected source, never an unrelated clash.
        cleanup_destination()
        raise
    finally:
        os.close(source_fd)


def apply_report(
    conn,
    report: dict,
    *,
    quarantine_root: str | Path,
    dry_run: bool = False,
    manifest_path: str | Path | None = None,
) -> dict:
    """Move report-listed duplicates into quarantine, never deleting anything.

    Every move rechecks the source SHA-256 first: missing, changed, or
    recently-modified (active) files are skipped, not forced.  ``dry_run``
    plans without moving or writing a manifest.  Returns a summary dict.
    """
    if not str(quarantine_root or "").strip():
        raise ValueError("a nonempty --quarantine-root is required")
    if not isinstance(report, dict) or "run_id" not in report \
            or "removable" not in report:
        raise ValueError("a dedupe report file is required")
    run = _run_row(conn, int(report["run_id"]))
    quarantine = Path(quarantine_root).resolve()
    run_corpus = Path(run["corpus_root"]).resolve()
    report_corpus = report.get("corpus_root")
    if report_corpus is None or Path(report_corpus).resolve() != run_corpus:
        raise ValueError("dedupe report corpus_root does not match its run")
    corpus = run_corpus
    if (quarantine == corpus or corpus in quarantine.parents
            or quarantine in corpus.parents):
        raise ValueError("quarantine root must be disjoint from the corpus root")
    try:
        report_time_ns = int(
            datetime.fromisoformat(
                report.get("created_at", "")).timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        raise ValueError("dedupe report has an invalid created_at timestamp")

    manifest_file = (Path(manifest_path) if manifest_path is not None
                     else quarantine / f"manifest-run-{run['id']}.json")
    run_identities = {
        item["path"]: item
        for item in _run_identities(conn, int(report["run_id"]), status=_OK)
    }
    entries: list[dict] = []
    skipped: dict[str, int] = defaultdict(int)
    for item in report["removable"]:
        entry = {"src": item.get("path", ""), "dst": "",
                 "sha256": item.get("sha256", ""),
                 "file_size": item.get("file_size", 0),
                 "cluster_id": item.get("cluster_id"),
                 "status": "planned", "reason": ""}
        entries.append(entry)
        src = Path(entry["src"])
        try:
            identity = run_identities.get(entry["src"])
            if identity is None or identity.get("sha256") != entry["sha256"]:
                entry.update(status="skipped", reason="not-in-run")
                skipped["not-in-run"] += 1
                continue
            resolved_src = src.resolve(strict=False)
            if corpus not in resolved_src.parents:
                entry.update(status="skipped", reason="outside-corpus")
                skipped["outside-corpus"] += 1
                continue
            if src.is_symlink():
                entry.update(status="skipped", reason="symlink")
                skipped["symlink"] += 1
                continue
            if quarantine in resolved_src.parents or resolved_src == quarantine:
                entry.update(status="skipped", reason="already-quarantined")
                skipped["already-quarantined"] += 1
                continue
        except (OSError, RuntimeError) as exc:
            entry.update(status="skipped", reason=f"path-check-failed: {exc}")
            skipped["path-check-failed"] += 1
            continue
        if not src.is_file():
            entry.update(status="skipped", reason="missing")
            skipped["missing"] += 1
            continue
        try:
            stat = src.stat()
            current_sha = sha256_of(src)
        except OSError as exc:
            entry.update(status="skipped", reason=f"unreadable: {exc}")
            skipped["unreadable"] += 1
            continue
        if current_sha != entry["sha256"]:
            entry.update(status="skipped", reason="changed")
            skipped["changed"] += 1
            continue
        if stat.st_mtime_ns > report_time_ns:
            entry.update(status="skipped", reason="active")
            skipped["active"] += 1
            continue
        dest = _collision_free_dest(
            quarantine, entry["sha256"], src.name, create_dirs=not dry_run)
        entry["dst"] = str(dest)
        if dest.exists():
            entry.update(status="skipped", reason="collision-same-content")
            skipped["collision-same-content"] += 1
    manifest = {
        "dedupe_tool_version": DEDUPE_TOOL_VERSION,
        "run_id": run["id"],
        "report_sha256": _canonical_sha(report),
        "report_created_at": report.get("created_at", ""),
        "corpus_root": str(corpus),
        "quarantine_root": str(quarantine),
        "created_at": _now(),
        "dry_run": dry_run,
        "moves": entries,
    }
    if dry_run:
        planned = sum(1 for entry in entries if entry["status"] == "planned")
        return {"moved": 0, "planned": planned,
                "skipped": dict(skipped), "manifest": None, "dry_run": True}
    quarantine.mkdir(parents=True, exist_ok=True)
    _write_manifest(manifest_file, manifest)
    moved = 0
    for entry in entries:
        if entry["status"] != "planned":
            continue
        entry["status"] = "moving"
        _write_manifest(manifest_file, manifest)
        try:
            _move_no_replace(Path(entry["src"]), Path(entry["dst"]),
                             entry["sha256"])
        except OSError as exc:
            entry.update(status="skipped", reason=f"move-failed: {exc}")
            skipped["move-failed"] += 1
            _write_manifest(manifest_file, manifest)
            continue
        entry.update(status="moved", reason="")
        moved += 1
        _write_manifest(manifest_file, manifest)
    manifest["completed_at"] = _now()
    _write_manifest(manifest_file, manifest)
    return {"moved": moved, "planned": 0, "skipped": dict(skipped),
            "manifest": str(manifest_file), "dry_run": False}


def rollback_manifest(manifest_path: str | Path) -> dict:
    """Restore exactly the moves one manifest records as ``moved``.

    An entry is restored only when its quarantine copy still exists and the
    original path is absent; anything else is a conflict that is recorded,
    never forced.  Collision-safe: restoring never overwrites.
    """
    with open(manifest_path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    moves = manifest.get("moves", []) if isinstance(manifest, dict) else []
    if not isinstance(manifest, dict) or not manifest.get("corpus_root"):
        return {"restored": 0,
                "conflicts": [{"src": "", "dst": "",
                               "reason": "missing-corpus-root"}],
                "conflict_count": 1}
    corpus = Path(manifest["corpus_root"]).resolve()
    quarantine = Path(manifest.get("quarantine_root", "")).resolve()
    restored = 0
    conflicts: list[dict] = []
    for entry in moves:
        if entry.get("status") not in {"moved", "moving"}:
            continue
        src, dst = Path(entry.get("src", "")), Path(entry.get("dst", ""))
        try:
            if corpus not in src.resolve(strict=False).parents:
                conflicts.append({"src": str(src), "dst": str(dst),
                                  "reason": "source-outside-corpus"})
                continue
            if quarantine not in dst.resolve(strict=False).parents:
                conflicts.append({"src": str(src), "dst": str(dst),
                                  "reason": "destination-outside-quarantine"})
                continue
        except OSError as exc:
            conflicts.append({"src": str(src), "dst": str(dst),
                              "reason": f"path-check-failed: {exc}"})
            continue
        if not str(dst) or not dst.is_file():
            conflicts.append({"src": str(src), "dst": str(dst),
                              "reason": "dst-missing"})
            continue
        if src.exists():
            conflicts.append({"src": str(src), "dst": str(dst),
                              "reason": "src-exists"})
            continue
        expected_sha = entry.get("sha256", "")
        try:
            if not expected_sha or sha256_of(dst) != expected_sha:
                conflicts.append({"src": str(src), "dst": str(dst),
                                  "reason": "hash-mismatch"})
                continue
        except OSError as exc:
            conflicts.append({"src": str(src), "dst": str(dst),
                              "reason": f"hash-failed: {exc}"})
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            _move_no_replace(dst, src, expected_sha)
        except OSError as exc:
            conflicts.append({"src": str(src), "dst": str(dst),
                              "reason": f"restore-failed: {exc}"})
            continue
        restored += 1
        # Keep the original ``moved`` state so a second rollback reports the
        # now-missing quarantine copy as a conflict instead of silently
        # claiming that there was nothing to do.
        entry.update(status="moved", reason="restored")
        _write_manifest(Path(manifest_path), manifest)
    _write_manifest(Path(manifest_path), manifest)
    return {"restored": restored, "conflicts": conflicts,
            "conflict_count": len(conflicts)}
