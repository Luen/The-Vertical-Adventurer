from waybackpy import WaybackMachineCDXServerAPI
import requests
from bs4 import BeautifulSoup
import os

user_agent = "Mozilla/5.0 (Windows NT 5.1; rv:40.0) Gecko/20100101 Firefox/40.0"
url = 'https://www.theverticaladventurer.com'
timestamp = '20221231235959'

cdx_api = WaybackMachineCDXServerAPI(
    url, user_agent, end_timestamp=timestamp)
snapshots = sorted(cdx_api.snapshots(),
                   key=lambda x: x.datetime_timestamp, reverse=True)

archive_url = snapshots[0].archive_url if snapshots else None

if not archive_url:
    print("No snapshot found before 2023.")
else:
    response = requests.get(archive_url)
    soup = BeautifulSoup(response.content, 'html.parser')

    # Create a directory to store the downloaded website
    directory = f"{url.split('//')[1].split('/')[0]}_{timestamp}"
    if not os.path.exists(directory):
        os.makedirs(directory)

    # Download and rewrite all links to point to the local directory
    visited_links = set()
    queue = [soup]
    while queue:
        page = queue.pop(0)
        for link in page.find_all('a'):
            if link.has_attr('href'):
                href = link['href']
                if href.startswith('http'):
                    # Ignore external links
                    continue
                elif href.startswith('/'):
                    # Fix links that start with a slash
                    href = url + href
                else:
                    # Fix relative links
                    href = url + '/' + href
                if href in visited_links:
                    # Ignore visited links
                    continue
                else:
                    visited_links.add(href)

                # Download the linked page and rewrite the link
                linked_page_cdx_api = WaybackMachineCDXServerAPI(
                    href, user_agent, end_timestamp=timestamp)
                linked_snapshots = sorted(linked_page_cdx_api.snapshots(
                ), key=lambda x: x.datetime_timestamp, reverse=True)

                if not linked_snapshots:
                    continue

                linked_page_url = linked_snapshots[0].archive_url
                linked_page_response = requests.get(linked_page_url)
                linked_page_soup = BeautifulSoup(
                    linked_page_response.content, 'html.parser')
                linked_page_file_path = os.path.join(
                    directory, href.split(url)[1].lstrip('/'))
                os.makedirs(os.path.dirname(
                    linked_page_file_path), exist_ok=True)
                with open(linked_page_file_path, 'w') as f:
                    f.write(linked_page_soup.prettify())
                link['href'] = linked_page_file_path
                queue.append(linked_page_soup)

    # Save the main page
    main_page_file_path = os.path.join(directory, 'index.html')
    with open(main_page_file_path, 'w') as f:
        f.write(soup.prettify())
