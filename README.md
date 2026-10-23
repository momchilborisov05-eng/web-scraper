# Book Store Web Scraper

A Python scraper that collects **~1000 books from all 50 categories** of [books.toscrape.com](https://books.toscrape.com) (a site made for scraping practice). It saves them to a **SQLite database** and a **formatted Excel report**.

## Features

- Collects **title, category, price, star rating, availability and URL** for every book.
- Follows **pagination** automatically in every category.
- **SQLite storage with UPSERT.** Re-running the scraper updates prices instead of creating duplicates, so you can track changes over time.
- **Excel report** with two sheets:
  - *Books*: all data, with header styling, filters, frozen header row and price formatting
  - *Summary by category*: book count, average / min / max price and average rating, calculated with SQL
- **Reliable.** Retries on network errors, has a timeout on every request and a polite delay between requests.
- Command-line options for a quick test run or custom output files.

## Tech stack

Python, Requests, BeautifulSoup, SQLite (`sqlite3`), openpyxl

## Setup and usage

```bash
pip install -r requirements.txt

python scraper.py                     # all categories (~1000 books, about 1 minute)
python scraper.py --categories 3      # quick test: first 3 categories
python scraper.py --db data.db --excel report.xlsx
```

Example output:

```
Found 3 categories.

[ 1/3] Travel                   11 books
[ 2/3] Mystery                  32 books
[ 3/3] Historical Fiction       26 books

Done: 69 books in 7s
  SQLite database: books.db
  Excel report:    books.xlsx
  Cheapest book:   Tastes Like Fear (DI Marnie Rome #3) (£10.69)
```

## Querying the data

```sql
-- Top 5 most expensive 5-star books
SELECT title, category, price_gbp
FROM books
WHERE rating = 5
ORDER BY price_gbp DESC
LIMIT 5;
```

## Adapting to other websites

The code is split into small parts: `get_categories`, `scrape_category`, `parse_book` and the save functions. To scrape another site, you usually only change the CSS selectors in `parse_book` and the `Book` fields.
