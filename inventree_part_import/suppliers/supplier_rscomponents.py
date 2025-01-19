import os
import pickle

from types import MethodType

from bs4 import BeautifulSoup

from ..error_helper import *
from ..retries import retry_timeouts
from .base import ApiPart, Supplier, SupplierSupportLevel, money2float
from .scrape import DOMAIN_REGEX, DOMAIN_SUB, REMOVE_HTML_TAGS, scrape

FALLBACK_DOMAINS = (
    "ca.rs-online.com",
    "uk.rs-online.com",
)


class RSComponents(Supplier):
    SUPPORT_LEVEL = SupplierSupportLevel.SCRAPING

    def setup(self, api_key, currency, scraping, locale_url="de.rs-online.com"):
        self.currency = currency
        self.use_scraping = scraping
        self.locale_url = locale_url

        return True


    def search(self, search_term):
        print(f"search_term: {search_term}  locale_url: {self.locale_url}  currency: {self.currency}")

        url = f"https://{self.locale_url}/web/c/?searchTerm={search_term}"
        filename = f"s_{search_term}.pkl".replace('/', '_')

        # check if we have a pickled result
        try:
            with open(filename, "rb") as f:
                print(f"Found '{search_term}' in search cache.")
                result = pickle.load(f)
        except:
            print(f"Retrieving search: '{search_term}'")
            if not (result := scrape(url, fallback_domains=FALLBACK_DOMAINS)):
                warning(f"Failed to cache '{search_term}' from '{url}' (blocked?)")
                return True
            # pickle result
            with open(filename, "wb") as f:
                pickle.dump(result, f)

        # parse search results
        soup = BeautifulSoup(result.content, "html.parser")

        # check if we went straight to the product page or have a result table
        results_table = soup.select("div[data-testid='product-tile-item']")
        if len(results_table) == 0:
            # e.g. inventree-part-import 136-1275
            matches = scrape_product_page(result, soup)
        else:
            # e.g. inventree-part-import ERJ-2RKF1002X
            matches = scrape_search_results(result, soup)

        # filter results
        search_term_norm = search_term.lower().replace('-', '')
        exact_matches = [
            rs_part for rs_part in matches
            if rs_part.get("RS stock no.", "").lower().replace('-', '').startswith(search_term_norm)
            or rs_part.get("Mfr. Part No.", "").lower().replace('-', '').startswith(search_term_norm)
        ]

        print(f"Got {len(matches)} results, filtered down to {len(matches)} results")

        # return exact matches, if we have them
        if len(exact_matches) > 0:
            return list(map(self.get_api_part, exact_matches)), len(exact_matches)

        # return all matches
        return list(map(self.get_api_part, matches)), len(matches)


    def get_api_part(self, rs_part):
        # If the part does not have any attributes it came from the
        # search results, so we need to also scrape the product page
        # for it.
        parameters = rs_part.get("ProductAttributes", {})
        if not len(parameters):
            rs_sku = rs_part.get("RS stock no.")
            rs_url = rs_part.get("ProductDetailUrl")
            print(f"Scraping RS product page for: {rs_sku} => {rs_url}")

            # check if we have a pickled result
            filename = f"p_{rs_sku}.pkl"
            try:
                with open(filename, "rb") as f:
                    print(f"Found '{rs_sku}' in search cache.")
                    result = pickle.load(f)
            except:
                print(f"Retrieving RSComponents SKU: '{rs_sku}'")
                if not (result := scrape(rs_url, fallback_domains=FALLBACK_DOMAINS)):
                    warning(f"Failed to cache '{rs_sku}' from '{rs_url}' (blocked?)")
                    return True
                # pickle result
                with open(filename, "wb") as f:
                    pickle.dump(result, f)

            # parse search results
            soup = BeautifulSoup(result.content, "html.parser")

            # scrape search results and update rs_part
            rs_part |= first(scrape_product_page(result, soup), rs_part)

        # debug
        print(f"Importing: {rs_part.get('Mfr. Part No.')} ({rs_part.get('RS stock no.')})")
        for field, value in rs_part.items():
            print(f"    {field}:\t{value}")

        # fix supplier link
        supplier_link = DOMAIN_REGEX.sub(
            DOMAIN_SUB.format(self.locale_url), rs_part.get("ProductDetailUrl"))

        # fix price breaks
        rs_price_breaks = rs_part.get("PriceBreaks", {})
        price_breaks = {}
        for qty, price in rs_price_breaks.items():
            qty = qty.split(' ', 1)[0]
            price = money2float(price)
            price_breaks[qty] = price
        print(f"PRICE BREAKS:")
        for qty, price in price_breaks.items():
            print(f"  {qty} : {price}")

        rs_stock_no = rs_part.get("RS stock no.")
        api_part = ApiPart(
            description        = rs_part.get("Description", ""),
            image_url          = rs_part.get("ImagePath"),
            datasheet_url      = rs_part.get("DataSheetUrl"),
            supplier_link      = supplier_link,
            SKU                = rs_stock_no,
            manufacturer       = rs_part.get("Manufacturer", ""),
            manufacturer_link  = "",
            MPN                = rs_part.get("Mfr. Part No.", rs_stock_no),
            quantity_available = float(rs_part.get("AvailabilityInStock", 0)),
            packaging          = rs_part.get("Packaging", ""),
            category_path      = rs_part.get("CategoryPath", ["Unknown"]),
            parameters         = parameters,
            price_breaks       = price_breaks,
            currency           = rs_part.get("Currency", self.currency),
        )

        return api_part


    def finalize_hook(self, api_part: ApiPart):
        print(f"finalize_hook({api_part})")
        return False
        return True


# - scrape product page -------------------------------------------------------

def scrape_product_page(result, soup):
    # TODO: brand-logo is data-testid='brand-logo'

    rs_part = {}
    try:
        # RS Part:
        #  RS stock no.      - key-details
        #  Mfr. Part No.     - key-details
        #  Manufacturer      - key-details
        key_details = first(soup.select("dl[data-testid='key-details-desktop']"))
        details = map(lambda column: column.text.strip().strip(":"), key_details.children)
        details = dict(zip(details, details))
        rs_part |= details

        # RS Part:
        #  Description       - long-description
        #  ProductDetailUrl  - result.url
        #  ImagePath         - gallery-content / https://media.rs-online.com/image/upload/R{rs_sku}-01
        description = first(soup.select("div[data-testid='long-description']"))
        product_detail_url = first(result.url.split('?', 1))
        rs_sku = details["RS stock no."]
        rs_sku_nodash = rs_sku.replace('-', '')

        gallery_content = soup.select_one("img[data-testid='gallery-fallback-image']")
        if gallery_content:
            image_path = gallery_content.get("src")
            image_path = first(image_path.split('?', 1))
            _, extension = os.path.splitext(image_path)
            if not extension:
                image_path += ".png"
        else:
            image_path = f"https://media.rs-online.com/image/upload/R{rs_sku_nodash}-01.jpg"

        rs_part |= {
            "Description":      description.text.strip(),
            "ProductDetailUrl": product_detail_url,
            "ImagePath":        image_path,
        }

        # RS Part:
        #  ProductAttributes - specification-attributes
        specification_attributes = first(soup.select("table[data-testid='specification-attributes']"))
        attributes = dict(
            tuple(
                map(
                    lambda column: column.text.strip().strip(":"),
                    row.find_all("td")[:2]
                )
            )
            for row in specification_attributes.find_all("tr")[1:]
        )
        rs_part |= { "ProductAttributes": attributes }

        # RS Part:
        #  PriceBreaks       - price-breaks
        #  Packaging         - price-breaks.header[2]
        #  Currency          - price-breaks.value.text[0]
        price_breaks = first(soup.select("table[data-testid='price-breaks']"))
        rs_price_breaks = dict(
            tuple(
                map(
                    lambda column: column.text.strip().replace('+', '').strip(),
                    row.find_all("td")[:2]
                )
            )
            for row in price_breaks.find_all("tr")[1:]
        )

        packaging = price_breaks.find("tr").find_all("th")
        if len(packaging) >= 3:
            packaging = packaging[2].find("div").text.split(" ")[-1]
            packaging = packaging.replace('*', '')
        else:
            packaging = ""

        currency_map = {
            "R": "ZAR",
            "€": "EUR",
            "$": "USD",
        }
        rs_currency = None
        qty, price = next(iter(rs_price_breaks.items()))
        if price:
            rs_currency = first([c for c in price if not c.isnumeric()])
            rs_currency = currency_map[rs_currency]

        rs_part |= {
            "PriceBreaks": rs_price_breaks,
            "Currency":    rs_currency,
            "Packaging":   packaging,
        }

        # RS Part:
        #  AvailabilityInStock - stock-status-0
        stock_status_0 = soup.select_one("div[data-testid='stock-status-0']")
        qty = first(stock_status_0.text.split(' '), 0)
        qty = float(qty) if qty.isnumeric() else 0.
        rs_part |= {
            "AvailabilityInStock": qty,
        }

        # RS Part:
        #  DataSheetUrl - technical-documents
        technical_documents = soup.select_one("ul[data-testid='technical-documents'] li a")
        rs_part |= {
            "DataSheetUrl": technical_documents.get("href", "")
        }

        # RS Part:
        #  Category - breadcrumb-container
        breadcrumb_container = soup.select_one("nav[data-testid='breadcrumb-container']")
        crumbs               = breadcrumb_container.select("a > span > span")
        category_path = [crumb.text for crumb in crumbs]
        rs_part |= {
            "CategoryPath": category_path,
        }

        return [rs_part]

    except Exception as e:
        warning(f"Failed to parse product page: {e}")
        return []


# - scrape search results -----------------------------------------------------

def scrape_search_results(result, soup):
    results_table = first(soup.select("div[data-testid='product-tile-item']"))

    results = []
    for index, entry in enumerate(results_table):
        try:
            product_tile_container  = first(entry.select("a[data-qa='product-tile-container']"))
            product_tile_title      = first(entry.select("div[data-qa='product-tile-title']"))
            product_tile_partno     = first(entry.select("div[data-qa='product-tile-partno-value']"))
            product_tile_mftr       = first(entry.select("div[data-qa='product-tile-mftr-value']"))
            product_tile_price      = first(entry.select("div[data-qa='product-tile-price']"))
            product_tile_price_unit = first(entry.select("div[data-qa='product-tile-price-unit']"))

            product_url  = first(product_tile_container.get("href", "").split('?', 1))
            partno_rs    = product_tile_partno.text.strip()
            partno_mftr  = product_tile_mftr.text.strip()
            description  = product_tile_title.text.strip()
            manufacturer = first(description.split(' ', 1)).strip()
            price        = product_tile_price.text.strip()
            price_unit   = product_tile_price_unit.text.strip()

            # RS Part:
            #  RS stock no.     - product-tile-partno
            #  Mfr. Part No.    - product-tile-mftr
            #  Manufacturer     - description (first word)
            #  Description      - description
            #  ProductDetailUrl - product-tile-container.href
            rs_part = {
                "RS stock no.":     partno_rs,
                "Mfr. Part No.":    partno_mftr,
                "Manufacturer":     manufacturer,
                "Description":      description,
                "ProductDetailUrl": product_url,
            }
            results.append(rs_part)
        except:
            continue

    return results


# - helpers -------------------------------------------------------------------

def first(l, fallback=None):
    return next(iter(l), fallback)
