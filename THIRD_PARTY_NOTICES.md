# Third-party notices

## MarketBrief

- Project: MarketBrief, https://github.com/yukipanpan/marketbrief
- License: MIT, Copyright (c) 2025 yukipan
- Used in: `core/news.py`
- What was adapted: the title normalisation, the Jaccard word-overlap de-duplication of headlines, the recency
  windows and the priority bucketing. The code was rewritten for this project (no random sampling, unknown dates are
  excluded and counted, every distinct source URL is kept); the structure follows MarketBrief.

```
MIT License

Copyright (c) 2025 yukipan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
