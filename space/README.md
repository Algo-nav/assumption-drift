---
title: assumption-drift
emoji: 📉
sdk: static
app_file: index.html
pinned: false
license: cc-by-4.0
---

# assumption-drift

Every numeric guidance statement in the SEC filings of 12 large US companies since 2019, matched to the reported figure and to any later filing that refers back to it. 100 worse than guided. 0 acknowledged. Of the 100 worse-side misses, 72 are more than 1% beyond the guide and 50 more than 5%. None was acknowledged.

| Statements | Resolved | Met | Missed | Worse | Better | Never acknowledged | Acknowledged |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,134 | 649 | 208 | 441 | 100 | 341 | 432 | 9 |

- Dataset: https://huggingface.co/datasets/Nav772/assumption-drift
- GitHub: https://github.com/Algo-nav/assumption-drift
- Write-up: https://github.com/Algo-nav/assumption-drift/blob/main/WRITEUP.md

Every row links both filings on EDGAR. The model proposed; code guards and a human reader decided. The counts of what the model got wrong are in the write-up.

Data CC-BY-4.0, code MIT.
