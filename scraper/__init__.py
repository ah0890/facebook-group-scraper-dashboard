"""Scraper engine.

Modules:
    models      – plain dataclasses (ScrapeConfig, ScrapedPost)
    exceptions  – error hierarchy used to decide retry / skip / abort
    browser     – Chromium lifecycle with a persistent Playwright profile
    facebook    – real-browser client + authenticate / check / URL-test tasks
    mock        – simulated client for demo & tests (SCRAPER_MODE=mock)
    parser      – raw feed data -> ScrapedPost (pure functions)
    storage     – buffered, deduplicating saves + JSON snapshot
    logger      – RunLogger writing ScraperLog rows for the live log panel
    runner      – execute_run(): the sequential run loop
    manager     – background thread management for the web UI
"""
