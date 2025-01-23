import os, sys, traceback
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
                return [], 0
            # pickle result
            with open(filename, "wb") as f:
                pickle.dump(result, f)

        # parse search results
        soup = BeautifulSoup(result.content, "html.parser")

        # check if we went straight to the product page or have a result table
        results_table = soup.select("div[data-testid='product-tile-item']")
        if len(results_table) == 0: # straight to product page?
            # e.g. inventree-part-import 136-1275
            matches = [scrape_product_page(result, soup, search_term)]
        else:
            # e.g. inventree-part-import ERJ-2RKF1002X
            matches = scrape_search_results(result, soup)

        #for index, rs_part in enumerate(matches):
        #    print(f"  {index} => {rs_part.get('Mfr. Part No.')}")

        # filter results
        search_term_lower = search_term.lower()
        search_term_norm  = search_term_lower.replace('-', '')
        mpn_lower = lambda rs_part: rs_part.get("Mfr. Part No.", "").lower()
        sku_norm  = lambda rs_part: rs_part.get("RS stock no.", "").lower().replace('-', '')

        filtered_matches = [
            rs_part for rs_part in matches
            if sku_norm(rs_part).startswith(search_term_norm)
            or mpn_lower(rs_part).startswith(search_term_lower)
        ]
        exact_matches = [
            rs_part for rs_part in filtered_matches
            if sku_norm(rs_part)  == search_term_norm or
               mpn_lower(rs_part) == search_term_lower
        ]
        def get_dupes(rs_parts):
            from collections import defaultdict
            dupes = defaultdict(list)
            for rs_part in rs_parts:
                dupes[mpn_lower(rs_part)].append(rs_part)
            print("counts: ", [k for k,v in dupes.items()])
            dupes = { k:v for k,v in dupes.items() if len(v) > 1 }
            return dupes
        identical_matches = get_dupes(filtered_matches)

        print(f"Got {len(matches)} results:")
        print(f"    {len(filtered_matches)} filtered matches")
        print(f"    {len(identical_matches)} identical matches")
        print(f"    {len(exact_matches)} exact matches")

        # return exact match, if we have one
        if len(exact_matches) == 1:
            print(f"HAVE AN EXACT MATCH: {exact_matches}")
            return [self.get_api_part(exact_matches[0])], 1

        # if we have matches with identical mpn's, first merge their price break data
        if len(identical_matches) > 0:
            print(f"HAVE IDENTICAL MATCHES: {len(identical_matches)}")

            def merge(rs_parts):
                # get price break data
                api_parts = list(map(self.get_api_part, rs_parts))
                lowest_qty_value = 10000
                lowest_qty_index = None
                price_breaks = {}
                for index, api_part in enumerate(api_parts):
                    price_break = api_part.price_breaks
                    #print(f"  #{index} => {price_break}")
                    for qty, price in price_break.items():
                        qty = int(first(qty.split(' ', 1), lowest_qty_value))
                        if qty < lowest_qty_value:
                            lowest_qty_index = index
                            lowest_qty_value = qty
                    price_breaks |= price_break
                api_part = api_parts[lowest_qty_index]
                api_part.price_breaks = price_breaks
                print(f"AUTO SELECTED: #{lowest_qty_index} => {api_part}")
                return api_part

            api_parts = [merge(rs_parts) for mpn, rs_parts in identical_matches.items()]
            #for index, api_part in enumerate(api_parts):
            #    print(f"#{index} => {api_part}")
            return api_parts, len(api_parts)
            #sys.exit(0)
            #return [rs_part], 1

        # otherwise, return filtered matches
        return list(map(self.get_api_part, filtered_matches)), len(filtered_matches)



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
                    return None
                # pickle result
                with open(filename, "wb") as f:
                    pickle.dump(result, f)

            # parse search results
            soup = BeautifulSoup(result.content, "html.parser")

            # scrape search results and update rs_part
            rs_part |= scrape_product_page(result, soup)

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

def scrape_product_page(result, soup, search_term=None):
    # TODO: brand-logo is data-testid='brand-logo'

    rs_part = {}
    try:
        # RS Part:
        #  RS stock no.      - key-details
        #  Mfr. Part No.     - key-details
        #  Manufacturer      - key-details
        key_details = soup.select_one("dl[data-testid='key-details-desktop']")
        if not key_details:
            warning(f"Failed to parse product page key details: {key_details}")
            return {}
        details = map(lambda column: column.text.strip().strip(":"), key_details.children)
        details = dict(zip(details, details))
        print(f"DETAILS: {details}")
        # fix details if needed
        try:
            details["RS stock no."] = details.pop("RS Stock No.")
        except:
            print(f"DETAILS: {details}")
        rs_part |= details

        # RS Part:
        #  Description       - long-description
        #  ProductDetailUrl  - result.url
        #  ImagePath         - gallery-content / https://media.rs-online.com/image/upload/R{rs_sku}-01
        description = soup.select_one("div[data-testid='long-description']")
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
        specification_attributes = soup.select_one("table[data-testid='specification-attributes']")
        attributes = {}
        if specification_attributes:
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
        price_breaks = soup.select_one("table[data-testid='price-breaks']")
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
            "£": "GBP",
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
        if stock_status_0:
            qty = first(stock_status_0.text.split(' '), 0)
            qty = float(qty) if qty.isnumeric() else 0.
            rs_part |= {
                "AvailabilityInStock": qty,
            }

        # RS Part:
        #  DataSheetUrl - technical-documents
        technical_documents = soup.select_one("ul[data-testid='technical-documents'] li a")
        if technical_documents:
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

        return rs_part

    except Exception as e:
        warning(f"Failed to parse product page: {e}")
        warning(traceback.format_exc())
        raise e
        #sys.exit(0)
        #return rs_part # TODO {} ?


# - scrape search results -----------------------------------------------------

def scrape_search_results(result, soup):
    results_table = soup.select("div[data-testid='product-tile-item']")

    results = []
    for index, entry in enumerate(results_table):
        try:
            product_tile_container  = entry.select_one("a[data-qa='product-tile-container']")
            product_tile_title      = entry.select_one("div[data-qa='product-tile-title']")
            product_tile_partno     = entry.select_one("div[data-qa='product-tile-partno-value']")
            product_tile_mftr       = entry.select_one("div[data-qa='product-tile-mftr-value']")
            product_tile_price      = entry.select_one("div[data-qa='product-tile-price']")
            product_tile_price_unit = entry.select_one("div[data-qa='product-tile-price-unit']")

            if product_tile_container:
                product_url  = first(product_tile_container.get("href", "").split('?', 1))
            if product_tile_partno:
                partno_rs    = product_tile_partno.text.strip()
            if product_tile_mftr:
                partno_mftr  = product_tile_mftr.text.strip()
            if product_tile_title:
                description  = product_tile_title.text.strip()
                manufacturer = first(description.split(' ', 1)).strip()
            if  product_tile_price:
                price        = product_tile_price.text.strip()
            if product_tile_price_unit:
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
        except Exception as e:
            warning(f"Failed to parse search page: {e}")
            warning(traceback.format_exc())
            raise e
            #sys.exit(0)
            #return rs_part # TODO {} ?

    return results


# - helpers -------------------------------------------------------------------

def first(l, fallback=None):
    return next(iter(l), fallback)
