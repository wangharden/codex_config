# Agents Notes

## Project
- Dev directory: D:\work\sell\result
- Clean/release directory: D:\work\sell\feedback\result

## Interaction Rules
- Think in English, reply in Chinese.
- User will ask in English.
- In every reply, repeat the user question in better English to help their grammar.

## Tools
- sync_feedback.ps1: Syncs source code from esult to eedback\\result.
- Usage (PowerShell):
  - cd D:\work\sell
  - ./sync_feedback.ps1 (code only)
  - ./sync_feedback.ps1 -IncludeConfig (also copy config.json, itpdk.ini)

## Learning / TODO
- Deepen understanding of TdfMarketDataApi::connect and callback flow.
- Practice callback patterns and multi-thread safety (global map + mutex, cache_mutex_).
- Refine sync_feedback.ps1 based on new ideas.

## Logs
- See .agents/session-log.md for chronological session notes.

