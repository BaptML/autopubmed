"""Pipeline `refresh_query` — le cœur du projet.

L'interface ne fait que lire la base ; tout le travail est ici. Les six étapes
suivent le §6 du cahier des charges : chercher, enrichir, qualifier, filtrer,
persister, marquer.

Usage :
  uv run python -m app.refresh --list
  uv run python -m app.refresh --new '"N Engl J Med"[ta] AND randomized controlled trial[pt]' --name "NEJM / RCT"
  uv run python -m app.refresh --id 1
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date

import httpx

from app import relevance, repo
from app.db import get_conn, init_db
from app.models import ArticleSummary, JournalQuality, SavedQuery
from app.openalex import OpenAlexClient
from app.pubmed import PubMedClient
from app.quality import passes_quality


@dataclass
class RefreshReport:
    """Trace d'un rafraîchissement, pensée pour le diagnostic."""

    query_name: str
    total_hits: int = 0  # ce que PubMed annonce au total
    fetched: int = 0  # PMID rapportés (plafonnés par retmax)
    newly_summarized: int = 0  # articles enrichis via ESummary
    unmatched_quality: int = 0  # articles dont aucun ISSN n'est dans journal_quality
    kept: int = 0
    rejected: int = 0
    too_old: int = 0  # écartés par l'ancienneté maximale de la requête
    in_future: int = 0  # écartés car datés après aujourd'hui (coquille / à paraître)
    new_links: int = 0  # paires (requête, article) inédites -> non lus
    translation: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Requête           : {self.query_name}",
            f"Traduction PubMed : {self.translation or '(aucune)'}",
            f"Résultats PubMed  : {self.total_hits} (dont {self.fetched} rapportés)",
            f"Enrichis (esummary): {self.newly_summarized}",
            f"Sans qualité connue: {self.unmatched_quality}",
            f"Retenus / écartés  : {self.kept} / {self.rejected}",
            f"Trop anciens       : {self.too_old}",
            f"Dans le futur      : {self.in_future}",
            f"Nouveaux (non lus) : {self.new_links}",
        ]
        for warning in self.warnings:
            lines.append(f"  ! avertissement PubMed : {warning}")
        for error in self.errors:
            lines.append(f"  !! erreur PubMed : {error}")
        return "\n".join(lines)


def _too_old(pub_date: str | None, cutoff: str | None) -> bool:
    """Article publié avant la date butoir de la requête.

    Même règle qu'en SQL (`repo._AGE_CLAUSE`) : les dates partielles de PubMed
    sont complétées vers la fin de période, et une date absente ne suffit pas à
    écarter un article.
    """
    if cutoff is None or not pub_date:
        return False
    return (pub_date + "-12-31")[:10] < cutoff


def _in_future(pub_date: str | None, today: str) -> bool:
    """Article daté après aujourd'hui — numéro « à paraître » ou coquille PubMed.

    Miroir de `_too_old` sur l'autre borne : la date partielle est complétée
    vers la fin de période (option stricte, comme `repo._NOT_FUTURE_CLAUSE`), et
    une date absente ne suffit pas à écarter l'article.
    """
    if not pub_date:
        return False
    return (pub_date + "-12-31")[:10] > today


def _best_quality(
    summary: ArticleSummary, quality_map: dict[str, JournalQuality]
) -> JournalQuality | None:
    """Verdict de la revue, en essayant l'ISSN papier puis l'électronique.

    Scimago n'indexe pas toujours celui que PubMed renvoie : sans cette
    double tentative, des revues classées passeraient pour non classées.
    """
    for issn in summary.issn_candidates():
        quality = quality_map.get(issn)
        if quality is not None:
            return quality
    return None


async def _fill_citedness(
    conn: sqlite3.Connection,
    summaries: list[ArticleSummary],
    quality_map: dict[str, JournalQuality],
) -> None:
    """Complète la citedness manquante depuis OpenAlex, une fois par revue."""
    issns = sorted({issn for s in summaries for issn in s.issn_candidates()})
    missing = repo.issns_missing_citedness(conn, issns)
    if not missing:
        return

    names = {
        issn: s.journal for s in summaries for issn in s.issn_candidates() if s.journal
    }
    async with OpenAlexClient() as client:
        for issn in missing:
            citedness = await client.get_citedness(issn)
            repo.set_citedness(conn, issn, citedness, names.get(issn))
            if citedness is not None:
                previous = quality_map.get(issn, JournalQuality())
                quality_map[issn] = JournalQuality(previous.quartile, citedness)


async def refresh_query(
    conn: sqlite3.Connection,
    query: SavedQuery,
    *,
    pubmed: PubMedClient,
    reldate_days: int | None = None,
) -> RefreshReport:
    """Rafraîchit une requête et renvoie le rapport de l'opération."""
    report = RefreshReport(query_name=query.name)

    # 1. Chercher — la chaîne part telle quelle, PubMed la valide.
    search = await pubmed.esearch(query.pubmed_query, reldate_days=reldate_days)
    report.total_hits = search.count
    report.fetched = len(search.pmids)
    report.translation = search.translation
    report.warnings = search.warnings
    report.errors = search.errors
    if not search.pmids:
        repo.mark_refreshed(conn, query.id)
        return report

    # 2. Enrichir — les PMID inconnus, plus ceux dont le DOI manque encore
    #    (colonne ajoutée après coup) ; jamais de résumé ici.
    already_known = repo.known_pmids(conn, search.pmids)
    to_refill = repo.pmids_missing_doi(conn, sorted(already_known))
    to_fetch = [
        pmid for pmid in search.pmids if pmid not in already_known or pmid in to_refill
    ]
    summaries = await pubmed.esummary(to_fetch) if to_fetch else []
    report.newly_summarized = len(summaries)
    for summary in summaries:
        repo.upsert_article(conn, summary)

    # Les articles réinterrogés sont déjà dans `summaries` : les reprendre ici
    # les compterait deux fois dans le rapport.
    refetched = {s.pmid for s in summaries}
    known_summaries = [
        s
        for s in (
            repo.get_article_summary(conn, pmid)
            for pmid in already_known
            if pmid not in refetched
        )
        if s is not None
    ]
    all_summaries = summaries + known_summaries

    # 3. Qualifier — quartile depuis Scimago, citedness à la demande.
    issns = sorted({issn for s in all_summaries for issn in s.issn_candidates()})
    quality_map = repo.get_quality_map(conn, issns)
    if query.citedness_min is not None:
        await _fill_citedness(conn, all_summaries, quality_map)
        quality_map = repo.get_quality_map(conn, issns)

    # 4. Filtrer puis 5. persister.
    cutoff = repo.cutoff_date(query.max_age_years)
    today = date.today().isoformat()
    for summary in all_summaries:
        if _too_old(summary.pub_date, cutoff):
            report.too_old += 1
            continue
        if _in_future(summary.pub_date, today):
            report.in_future += 1
            continue
        quality = _best_quality(summary, quality_map)
        if quality is None:
            report.unmatched_quality += 1
        if not passes_quality(quality, query.quartile_min, query.citedness_min):
            report.rejected += 1
            continue
        report.kept += 1
        if repo.link_article(conn, query.id, summary.pmid):
            report.new_links += 1

    # 6. Marquer.
    repo.mark_refreshed(conn, query.id)

    # 7. Prédire la pertinence des nouveaux venus. Après le rattachement, et
    #    sans effet sur lui : la couche pertinence ne s'applique qu'à l'affichage.
    relevance.recompute_scores(conn, query.id)
    return report


async def refresh_all(conn: sqlite3.Connection) -> list[RefreshReport]:
    """Rafraîchit toutes les requêtes, séquentiellement (étiquette API).

    Une requête dont l'appel réseau échoue n'interrompt pas les suivantes :
    l'erreur atterrit dans son rapport. Indispensable au job de fond, qui
    tourne sans personne pour le relancer.

    Un commit par requête : sans lui, le verrou d'écriture SQLite resterait
    posé pendant tout le passage, et un clic « lu » ou « favori » dans
    l'interface attendrait jusqu'à échouer sur « database is locked ».
    """
    reports: list[RefreshReport] = []
    async with PubMedClient() as pubmed:
        for query in repo.list_queries(conn):
            try:
                reports.append(await refresh_query(conn, query, pubmed=pubmed))
            except httpx.HTTPError as exc:
                report = RefreshReport(query_name=query.name)
                report.errors.append(str(exc) or exc.__class__.__name__)
                reports.append(report)
            conn.commit()
    return reports


# --- Interface en ligne de commande --------------------------------------


def _print_articles(conn: sqlite3.Connection, query_id: int, limit: int) -> None:
    rows = conn.execute(
        """
        SELECT a.pmid, a.title, a.journal, a.pub_date, qa.read,
               (SELECT MIN(jq.quartile) FROM journal_quality jq
                 WHERE jq.issn IN (a.issn, a.eissn)) AS quartile,
               (SELECT MAX(jq.citedness) FROM journal_quality jq
                 WHERE jq.issn IN (a.issn, a.eissn)) AS citedness
        FROM query_articles qa JOIN articles a ON a.pmid = qa.pmid
        WHERE qa.query_id = ?
        ORDER BY a.pub_date DESC
        LIMIT ?
        """,
        (query_id, limit),
    ).fetchall()

    print(f"\n{len(rows)} article(s) affiché(s) :")
    for row in rows:
        quartile = f"Q{row['quartile']}" if row["quartile"] else "non classée"
        citedness = f" · citedness {row['citedness']:.1f}" if row["citedness"] else ""
        flag = " " if row["read"] else "•"
        print(
            f"{flag} [{row['pub_date'] or '?'}] {row['journal']} ({quartile}{citedness})"
        )
        print(f"    {row['title'][:110]}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Rafraîchir une requête PubMed")
    parser.add_argument("--list", action="store_true", help="lister les requêtes")
    parser.add_argument("--id", type=int, help="rafraîchir la requête d'identifiant N")
    parser.add_argument(
        "--all", action="store_true", help="rafraîchir toutes les requêtes"
    )
    parser.add_argument(
        "--new", metavar="CHAINE", help="créer une requête puis la rafraîchir"
    )
    parser.add_argument("--name", help="libellé de la requête créée")
    parser.add_argument("--folder", default="Sans dossier")
    parser.add_argument("--quartile-min", type=int, choices=[1, 2, 3, 4])
    parser.add_argument("--citedness-min", type=float)
    parser.add_argument("--reldate", type=int, help="fenêtre de récence en jours")
    parser.add_argument("--show", type=int, default=10, help="articles à afficher")
    args = parser.parse_args(argv)

    init_db()

    with get_conn() as conn:
        if args.list:
            queries = repo.list_queries(conn)
            if not queries:
                print("Aucune requête enregistrée.")
            for query in queries:
                filters = []
                if query.quartile_min:
                    filters.append(f"Q<={query.quartile_min}")
                if query.citedness_min:
                    filters.append(f"citedness>={query.citedness_min}")
                suffix = f" [{', '.join(filters)}]" if filters else ""
                print(f"{query.id:3d} · {query.folder} / {query.name}{suffix}")
                print(f"      {query.pubmed_query}")
            return 0

        if args.new:
            if not args.name:
                print("--name est requis avec --new", file=sys.stderr)
                return 1
            query_id = repo.create_query(
                conn,
                name=args.name,
                pubmed_query=args.new,
                folder=args.folder,
                quartile_min=args.quartile_min,
                citedness_min=args.citedness_min,
            )
        elif args.id:
            query_id = args.id
        elif args.all:
            reports = asyncio.run(refresh_all(conn))
            for report in reports:
                print(report.render())
                print()
            return 0
        else:
            parser.print_help()
            return 1

        query = repo.get_query(conn, query_id)
        if query is None:
            print(f"Requête introuvable : {query_id}", file=sys.stderr)
            return 1

        async def run() -> RefreshReport:
            async with PubMedClient() as pubmed:
                return await refresh_query(
                    conn, query, pubmed=pubmed, reldate_days=args.reldate
                )

        report = asyncio.run(run())
        print(report.render())
        if args.show:
            _print_articles(conn, query_id, args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
