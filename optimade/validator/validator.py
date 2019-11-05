""" This module contains a validator class that can be pointed
at an OPTiMaDe implementation and validated against the pydantic
models in this package.

It is written with the pytest framework to provide a detailed
breakdown of any issues.

"""

import sys
import time
import requests
import logging
import argparse
import traceback

from pydantic import ValidationError
from matador.utils.print_utils import print_success, print_warning, print_failure

from optimade.models import (
    InfoResponse,
    StructureResponseOne,
    StructureResponseMany,
    EntryInfoResponse,
    ManualValidationError,
)

MAX_RETRIES = 5

BASE_INFO_ENDPOINT = "info"
REQUIRED_ENTRY_ENDPOINTS = ["structures"]

RESPONSE_CLASSES = {
    "structures": StructureResponseMany,
    "structures/": StructureResponseOne,
    "calculations": StructureResponseMany,
    "calculations/": StructureResponseOne,
    "info": InfoResponse,
    "info/structures": EntryInfoResponse,
}


class ResponseError(Exception):
    """ This exception should be raised for a manual hardcoded test failure. """

    pass


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.last_request = None
        self.response = None

    def get(self, request: str):
        self.last_request = f"{self.base_url}/{request}"
        status_code = None
        retries = 0
        # probably a smarter way to do this with requests, but their documentation 404's...
        while retries < MAX_RETRIES:
            retries += 1
            self.response = requests.get(self.last_request)
            status_code = self.response.status_code
            if status_code != 429:
                break

            print("Hit rate limit, sleeping for 1 s...")
            time.sleep(1)

        else:
            raise ResponseError("Hit max (manual) retries on request.")

        return self.response


def test_case(test_fn):
    from functools import wraps

    @wraps(test_fn)
    def wrapper(*args, **kwargs):
        try:
            result, msg = test_fn(*args, **kwargs)
        except (ResponseError, ValidationError, ManualValidationError) as exc:
            result = False
            msg = f"{type(exc).__name__}: {exc}"

        try:
            request = args[0].client.last_request
        except AttributeError:
            request = args[0].base_url
        if result:
            args[0].success_count += 1
            if args[0].verbosity > 0:
                print_success(f"✔: {request} - {msg}")
        else:
            args[0].failure_count += 1
            print_failure(f"✖: {request} - failed with error")
            message = f"{msg}".split("\n")
            for line in message:
                print_warning(f"\t{line}")

        return result

    return wrapper


class ImplementationValidator:
    def __init__(self, client=None, base_url=None, verbosity=0, *args, **kwargs):

        if client is None and base_url is None:
            raise RuntimeError(
                "Need at least a URL or a client to initialize validator."
            )
        if base_url and client:
            raise RuntimeError("Please specify at most one of base_url or client.")
        if client:
            self.client = client
            self.base_url = self.client.base_url
        else:
            self.base_url = base_url
            self.client = Client(base_url)

        self.test_id_by_type = {}
        self.verbosity = verbosity
        self._setup_log()
        self.base_info_endpoint = BASE_INFO_ENDPOINT
        self.expected_entry_endpoints = REQUIRED_ENTRY_ENDPOINTS
        self.test_entry_endpoints = set(self.expected_entry_endpoints)
        # if True on exit, script returns 0 to shell
        # if False on exit, script returns 1 to shell
        # if None on exit, script returns 2 to shell, indicating an internal failure
        self.valid = None
        self.success_count = 0
        self.failure_count = 0

    def _setup_log(self):
        self.log = logging.getLogger("validator")
        self.log.handlers = []
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s | %(levelname)8s: %(message)s")
        )
        self.log.addHandler(stdout_handler)
        if self.verbosity == 0:
            self.log.setLevel(logging.CRITICAL)
        elif self.verbosity == 1:
            self.log.setLevel(logging.INFO)
        else:
            self.log.setLevel(logging.DEBUG)

    def main(self):
        self.log.info("Testing {}...".format(self.base_url))

        self.log.debug("Testing base info endpoint of {}".format(BASE_INFO_ENDPOINT))
        base_info = self.test_info_endpoints(BASE_INFO_ENDPOINT)
        self.get_available_endpoints(base_info)

        self.log.debug(
            "Testing for expected info endpoints {}".format(BASE_INFO_ENDPOINT)
        )
        for endp in self.test_entry_endpoints:
            entry_info_endpoint = f"{BASE_INFO_ENDPOINT}/{endp}"
            self.log.debug(
                "Testing expected info endpoints".format(entry_info_endpoint)
            )
            self.test_info_endpoints(entry_info_endpoint)
        self.log.debug(
            "Testing for expected info endpoints {}".format(BASE_INFO_ENDPOINT)
        )

        for endp in self.test_entry_endpoints:
            self.log.debug("Testing multiple entry endpoint of {}".format(endp))
            self.test_multi_entry_endpoint(endp)

        for endp in self.test_entry_endpoints:
            self.log.debug("Testing single entry request of type {}".format(endp))
            self.test_single_entry_endpoint(endp)

        self.valid = not bool(self.failure_count)

        self.log.info(
            f"Passed {self.success_count} out of {self.success_count + self.failure_count} tests."
        )

    @test_case
    def serialize_attempt(self, response, response_cls):
        if not response:
            raise ResponseError("Request failed")
        return (
            response_cls(**response.json()),
            "serialized correctly as {}".format(response_cls),
        )

    @test_case
    def get_available_endpoints(self, base_info):
        """ Try to get `entry_types_by_format` even if base info response could not be validated. """
        # hopefully just a temporary hack...
        for i in [0]:
            available_json_entry_endpoints = []
            try:
                available_json_entry_endpoints = base_info.data.attributes.entry_types_by_format.get(
                    "json"
                )
                break
            except Exception:
                self.log.warning(
                    "Info endpoint failed serialization, trying to manually extract entry_types_by_format."
                )

            if not base_info.json():
                raise ResponseError("Unable to get entry types from base info endpoint")

            try:
                available_json_entry_endpoints = base_info.json()["data"]["attributes"][
                    "entry_types_by_format"
                ]["json"]
                break
            except (KeyError, TypeError):
                raise ResponseError(
                    "Unable to get entry_types_by_format from unserializable base info response {}.".format(
                        base_info
                    )
                )
        else:
            raise ResponseError(
                "Unable to find any JSON entry types in entry_types_by_format"
            )

        self.test_entry_endpoints |= set(available_json_entry_endpoints)
        if "info" in self.test_entry_endpoints:
            raise ResponseError(
                'Illegal entry "info" was found in entry_types_by_format"'
            )
        return (
            available_json_entry_endpoints,
            "successfully found available entry types in baseinfo",
        )

    @test_case
    def get_endpoint(self, request_str):
        response = self.client.get(request_str)
        if response.status_code != 200:
            raise ResponseError(
                "Request to endpoint {} returned {}".format(
                    request_str, response.status_code
                )
            )
        return response, "request successful."

    def test_info_endpoints(self, request_str):
        response = self.get_endpoint(request_str)
        if response:
            serialized = self.serialize_attempt(response, RESPONSE_CLASSES[request_str])
            if not serialized:
                return response
            return serialized
        return False

    def test_multi_entry_endpoint(self, request_str):
        response = self.get_endpoint(request_str)
        serialized = self.serialize_attempt(response, RESPONSE_CLASSES[request_str])
        self.get_single_id_from_multi_endpoint(serialized)

    @test_case
    def get_single_id_from_multi_endpoint(self, serialized):
        if serialized and len(serialized.data) > 0:
            self.test_id_by_type[serialized.data[0].type] = serialized.data[0].id
            self.log.debug(
                "Set type {} test ID to {}".format(
                    serialized.data[0].type, serialized.data[0].id
                )
            )
        else:
            raise ResponseError("No entries found under endpoint to scrape ID from.")
        return (
            self.test_id_by_type[serialized.data[0].type],
            f"successfully scraped test ID from {serialized.data[0].type} endpoint",
        )

    def test_single_entry_endpoint(self, _type):
        if _type in self.test_id_by_type:
            response = self.get_endpoint(f"{_type}/{test_id}")
            if response:
                self.serialize_attempt(response, RESPONSE_CLASSES[_type + "/"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="http://localhost:5000")
    parser.add_argument(
        "--verbosity", "-v", type=int, default=0, help="The verbosity of the output"
    )
    args = vars(parser.parse_args())

    validator = ImplementationValidator(
        base_url=args["base_url"], verbosity=args["verbosity"]
    )

    try:
        validator.main()
    # catch and print internal exceptions, exiting with non-zero error code
    except Exception:
        traceback.print_exc()

    if validator.valid is None:
        sys.exit(2)
    elif not validator.valid:
        sys.exit(1)
