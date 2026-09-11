"""Guarded collection of explicitly selected first-party article pages."""

from ai_daily_digest.ingestion.article.collector import collect_official_articles

__all__ = ["collect_official_articles"]
