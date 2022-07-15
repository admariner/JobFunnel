"""Scraper designed to get jobs from www.indeed.X
"""

import re
from concurrent.futures import ThreadPoolExecutor, wait
from math import ceil
from typing import Any, Dict, List, Optional
from unicodedata import normalize

from bs4 import BeautifulSoup
from requests import Session

from jobfunnel.backend import Job
from jobfunnel.backend.scrapers.base import (BaseCANEngScraper, BaseScraper,
                                             BaseUSAEngScraper,
                                             BaseUKEngScraper,
                                             BaseFRFreScraper,
                                             BaseDEGerScraper)
from jobfunnel.backend.tools.filters import JobFilter
from jobfunnel.backend.tools.tools import calc_post_date_from_relative_str
from jobfunnel.resources import MAX_CPU_WORKERS, JobField, Remoteness

# pylint: enable=using-constant-test,unused-import

ID_REGEX = re.compile(r'id=\"sj_([a-zA-Z0-9]*)\"')
MAX_RESULTS_PER_INDEED_PAGE = 50
# NOTE: these magic strings stick for both the US and CAN indeed websites...
FULLY_REMOTE_MAGIC_STRING = "&remotejob=032b3046-06a3-4876-8dfd-474eb5e7ed11"
COVID_REMOTE_MAGIC_STRING = "&remotejob=7e3167e4-ccb4-49cb-b761-9bae564a0a63"
REMOTENESS_TO_QUERY = {
    Remoteness.IN_PERSON: '',
    Remoteness.TEMPORARILY_REMOTE: COVID_REMOTE_MAGIC_STRING,
    Remoteness.PARTIALLY_REMOTE: '',
    Remoteness.FULLY_REMOTE: FULLY_REMOTE_MAGIC_STRING,
    Remoteness.ANY: '',
}
REMOTENESS_STR_MAP = {
    'remote': Remoteness.FULLY_REMOTE,
    'temporarily remote': Remoteness.TEMPORARILY_REMOTE,
}


class BaseIndeedScraper(BaseScraper):
    """Scrapes jobs from www.indeed.X
    """

    def __init__(self, session: Session, config: 'JobFunnelConfigManager',
                 job_filter: JobFilter) -> None:
        """Init that contains indeed specific stuff
        """
        super().__init__(session, config, job_filter)
        self.max_results_per_page = MAX_RESULTS_PER_INDEED_PAGE
        self.query = '+'.join(self.config.search_config.keywords)

        # Log if we can't do their remoteness query (Indeed only has 2 lvls.)
        if self.config.search_config.remoteness == Remoteness.PARTIALLY_REMOTE:
            self.logger.warning("Indeed does not support PARTIALLY_REMOTE jobs")

    @property
    def job_get_fields(self) -> str:
        """Call self.get(...) for the JobFields in this list when scraping a Job

        Override this as needed.
        """
        return [
            JobField.TITLE, JobField.COMPANY, JobField.LOCATION,
            JobField.KEY_ID, JobField.TAGS, JobField.POST_DATE,
            JobField.REMOTENESS, JobField.WAGE,
        ]

    @property
    def job_set_fields(self) -> str:
        """Call self.set(...) for the JobFields in this list when scraping a Job

        NOTE: Since this passes the Job we are updating, the order of this list
        matters if set fields rely on each-other.

        Override this as needed.
        """
        return [JobField.RAW, JobField.URL, JobField.DESCRIPTION]

    @property
    def delayed_get_set_fields(self) -> str:
        """Delay execution when getting /setting any of these attributes of a
        job.

        Override this as needed.
        """
        return [JobField.RAW]

    @property
    def high_priority_get_set_fields(self) -> List[JobField]:
        """These get() and/or set() fields will be populated first.
        """
        return [JobField.URL]

    @property
    def headers(self) -> Dict[str, str]:
        """Session header for indeed.X
        """
        return {
            'accept': 'text/html,application/xhtml+xml,application/xml;'
            'q=0.9,image/webp,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate, sdch',
            'accept-language': 'en-GB,en-US;q=0.8,en;q=0.6',
            'referer':
                f'https://www.indeed.{self.config.search_config.domain}/',
            'upgrade-insecure-requests': '1',
            'user-agent': self.user_agent,
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }

    def get_job_soups_from_search_result_listings(self) -> List[BeautifulSoup]:
        """Scrapes raw data from a job source into a list of job-soups

        Returns:
            List[BeautifulSoup]: list of jobs soups we can use to make Job init
        """
        # Get the search url
        search_url = self._get_search_url()

        # Parse total results, and calculate the # of pages needed
        pages = self._get_num_search_result_pages(search_url)
        self.logger.info(
            "Found %d pages of search results for query=%s", pages, self.query
        )

        # Init list of job soups
        job_soup_list = []  # type: List[Any]

        # Init threads & futures list FIXME: we should probably delay here too
        threads = ThreadPoolExecutor(max_workers=MAX_CPU_WORKERS)
        try:
            # Scrape soups for all the result pages containing many job listings
            futures = [
                threads.submit(
                    self._get_job_soups_from_search_page,
                    search_url,
                    page,
                    job_soup_list,
                )
                for page in range(pages)
            ]

            # Wait for all scrape jobs to finish
            wait(futures)

        finally:
            threads.shutdown()

        return job_soup_list

    def get(self, parameter: JobField, soup: BeautifulSoup) -> Any:
        """Get a single job attribute from a soup object by JobField
        """
        if parameter == JobField.TITLE:
            return soup.find(
                'a', attrs={'data-tn-element': 'jobTitle'}
            ).text.strip()
        elif parameter == JobField.COMPANY:
            return soup.find('span', attrs={'class': 'company'}).text.strip()
        elif parameter == JobField.LOCATION:
            return soup.find('span', attrs={'class': 'location'}).text.strip()
        elif parameter == JobField.TAGS:
            if table_soup := soup.find(
                'table', attrs={'class': 'jobCardShelfContainer'}
            ):
                return [
                    td.text.strip() for td in table_soup.find_all(
                        'td', attrs={'class': 'jobCardShelfItem'}
                    )
                ]
            else:
                return []
        elif parameter == JobField.REMOTENESS:
            if remote_field := soup.find('span', attrs={'class': 'remote'}):
                remoteness_str = remote_field.text.strip().lower()
                if remoteness_str in REMOTENESS_STR_MAP:
                    return REMOTENESS_STR_MAP[remoteness_str]
            return Remoteness.UNKNOWN
        elif parameter == JobField.WAGE:
            if potential := soup.find('span', attrs={'class': 'salaryText'}):
                return potential.text.strip()
            else:
                return ''
        elif parameter == JobField.POST_DATE:
            return calc_post_date_from_relative_str(
                soup.find('span', attrs={'class': 'date'}).text.strip()
            )
        elif parameter == JobField.KEY_ID:
            return ID_REGEX.findall(
                str(
                    soup.find(
                        'a', attrs={'class': 'sl resultLink save-job-link'}
                    )
                )
            )[0]
        else:
            raise NotImplementedError(f"Cannot get {parameter.name}")

    def set(self, parameter: JobField, job: Job, soup: BeautifulSoup) -> None:
        """Set a single job attribute from a soup object by JobField
        NOTE: URL is high-priority, since we need it to get RAW.
        """
        if parameter == JobField.RAW:
            job._raw_scrape_data = BeautifulSoup(
                self.session.get(job.url).text, self.config.bs4_parser
            )
        elif parameter == JobField.DESCRIPTION:
            assert job._raw_scrape_data
            job.description = job._raw_scrape_data.find(
                id='jobDescriptionText'
            ).text.strip()
        elif parameter == JobField.URL:
            assert job.key_id
            job.url = (
                f"http://www.indeed.{self.config.search_config.domain}/"
                f"viewjob?jk={job.key_id}"
            )
        else:
            raise NotImplementedError(f"Cannot set {parameter.name}")

    def _get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        TODO: use Enum for method instead of str.
        """
        if method == 'get':
            return f"https://www.indeed.{self.config.search_config.domain}/jobs?q={self.query}&l={self.config.search_config.city.replace(' ', '+')}%2C+{self.config.search_config.province_or_state.upper()}&radius={self._quantize_radius(self.config.search_config.radius)}&limit={self.max_results_per_page}&filter={int(self.config.search_config.return_similar_results)}{REMOTENESS_TO_QUERY[self.config.search_config.remoteness]}"

        elif method == 'post':
            raise NotImplementedError()
        else:
            raise ValueError(f'No html method {method} exists')

    def _quantize_radius(self, radius: int) -> int:
        """Quantizes the user input radius to a valid radius value into:
        5, 10, 15, 25, 50, 100, and 200 kilometers or miles.
        TODO: implement with numpy instead of if/else cases.
        """
        if radius < 5:
            return 0
        elif 5 <= radius < 10:
            return 5
        elif 10 <= radius < 15:
            return 10
        elif 15 <= radius < 25:
            return 15
        elif 25 <= radius < 50:
            return 25
        elif 50 <= radius < 100:
            return 50
        else:
            return 100

    def _get_job_soups_from_search_page(self, search: str, page: str,
                                        job_soup_list: List[BeautifulSoup]
                                        ) -> None:
        """Scrapes the indeed page for a list of job soups
        NOTE: modifies the job_soup_list in-place
        NOTE: Indeed's remoteness filter sucks, and we will always see a mix.
            ... need to add some kind of filtering for this!
        """
        url = f'{search}&start={int(page * self.max_results_per_page)}'
        job_soup_list.extend(
            BeautifulSoup(
                self.session.get(url).text, self.config.bs4_parser
            ).find_all('div', attrs={'data-tn-component': 'organicJob'})
        )

    def _get_num_search_result_pages(self, search_url: str, max_pages=0) -> int:
        """Calculates the number of pages of job listings to be scraped.

        i.e. your search yields 230 results at 50 res/page -> 5 pages of jobs

        Args:
			max_pages: the maximum number of pages to be scraped.
        Returns:
            The number of pages to be scraped.
        """
        # Get the html data, initialize bs4 with lxml
        request_html = self.session.get(search_url)
        self.logger.debug(
            "Got Base search results page: %s", search_url
        )
        query_resp = BeautifulSoup(request_html.text, self.config.bs4_parser)
        num_res = query_resp.find(id='searchCountPages')
        # TODO: we should consider expanding the error cases (scrape error page)
        if not num_res:
            raise ValueError(
                f"Unable to identify number of pages of results for query: {search_url} Please ensure linked page contains results, you may have provided a city for which there are no results within this province or state."
            )


        num_res = num_res.contents[0].strip()
        num_res = int(re.findall(r'f (\d+) ', num_res.replace(',', ''))[0])
        number_of_pages = int(ceil(num_res / self.max_results_per_page))
        if max_pages == 0 or number_of_pages < max_pages:
            return number_of_pages
        else:
            return max_pages


class IndeedScraperCANEng(BaseIndeedScraper, BaseCANEngScraper):
    """Scrapes jobs from www.indeed.ca
    """


class IndeedScraperUSAEng(BaseIndeedScraper, BaseUSAEngScraper):
    """Scrapes jobs from www.indeed.com
    """


class IndeedScraperUKEng(BaseIndeedScraper, BaseUKEngScraper):
    """Scrapes jobs from www.indeed.co.uk
    """
    def _get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        TODO: use Enum for method instead of str.
        """
        if method == 'get':
            return f"https://www.indeed.{self.config.search_config.domain}/jobs?q={self.query}&l={self.config.search_config.city.replace(' ', '+')}&radius={self._quantize_radius(self.config.search_config.radius)}&limit={self.max_results_per_page}&filter={int(self.config.search_config.return_similar_results)}{REMOTENESS_TO_QUERY[self.config.search_config.remoteness]}"

        elif method == 'post':
            raise NotImplementedError()
        else:
            raise ValueError(f'No html method {method} exists')


class IndeedScraperFRFre(BaseIndeedScraper, BaseFRFreScraper):
    """Scrapes jobs from www.indeed.fr
    """
    def _get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        TODO: use Enum for method instead of str.
        """
        if method == 'get':
            return f"https://www.indeed.{self.config.search_config.domain}/jobs?q={self.query}&l={self.config.search_config.city.replace(' ', '+')}+%28{self.config.search_config.province_or_state.upper()}%29&radius={self._quantize_radius(self.config.search_config.radius)}&limit={self.max_results_per_page}&filter={int(self.config.search_config.return_similar_results)}{REMOTENESS_TO_QUERY[self.config.search_config.remoteness]}"

        elif method == 'post':
            raise NotImplementedError()
        else:
            raise ValueError(f'No html method {method} exists')


    def _get_num_search_result_pages(self, search_url: str, max_pages=0) -> int:
        """Calculates the number of pages of job listings to be scraped.

        i.e. your search yields 230 results at 50 res/page -> 5 pages of jobs

        Args:
			max_pages: the maximum number of pages to be scraped.
        Returns:
            The number of pages to be scraped.
        """
        # Get the html data, initialize bs4 with lxml
        request_html = self.session.get(search_url)
        self.logger.debug(
            "Got Base search results page: %s", search_url
        )
        query_resp = BeautifulSoup(request_html.text, self.config.bs4_parser)
        num_res = query_resp.find(id='searchCountPages')
        # TODO: we should consider expanding the error cases (scrape error page)
        if not num_res:
            raise ValueError(
                f"Unable to identify number of pages of results for query: {search_url} Please ensure linked page contains results, you may have provided a city for which there are no results within this province or state."
            )


        num_res = normalize("NFKD", num_res.contents[0].strip())
        num_res = int(re.findall(r'(\d+) ', num_res.replace(',', ''))[1])
        number_of_pages = int(ceil(num_res / self.max_results_per_page))
        if max_pages == 0 or number_of_pages < max_pages:
            return number_of_pages
        else:
            return max_pages


class IndeedScraperDEGer(BaseIndeedScraper, BaseDEGerScraper):
    """Scrapes jobs from de.indeed.com
    """

    # The german locale has a different number separators.
    THOUSEP = "."

    def _get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        """
        if method == 'get':
            return f"https://{self.config.search_config.domain}.indeed.com/jobs?q={self.query}&l={self.config.search_config.city.replace(' ', '+')}&radius={self._quantize_radius(self.config.search_config.radius)}&limit={self.max_results_per_page}&filter={int(self.config.search_config.return_similar_results)}{REMOTENESS_TO_QUERY[self.config.search_config.remoteness]}"

        elif method == 'post':
            raise NotImplementedError()
        else:
            raise ValueError(f'No html method {method} exists')

    def _get_num_search_result_pages(self, search_url: str, max_pages=0) -> int:
        """Calculates the number of pages of job listings to be scraped.

        i.e. your search yields 230 results at 50 res/page -> 5 pages of jobs

        Args:
            max_pages: the maximum number of pages to be scraped.
        Returns:
            The number of pages to be scraped.
        """
        # Get the html data, initialize bs4 with lxml
        request_html = self.session.get(search_url)
        self.logger.debug(
            "Got Base search results page: %s", search_url
        )
        query_resp = BeautifulSoup(request_html.text, self.config.bs4_parser)
        num_res = query_resp.find(id='searchCountPages')
        if not num_res:
            raise ValueError(
                f"Unable to identify number of pages of results for query: {search_url} Please ensure linked page contains results, you may have provided a city for which there are no results within this province or state."
            )


        num_res = num_res.contents[0].strip()
        num_res = int(re.findall(r'(\d+)', num_res.replace(self.THOUSEP, ''))[1])
        number_of_pages = int(ceil(num_res / self.max_results_per_page))
        if max_pages == 0 or number_of_pages < max_pages:
            return number_of_pages
        else:
            return max_pages
