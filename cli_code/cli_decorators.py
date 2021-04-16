"""Module for all decorators related to the execution of the DDS CLI."""

###############################################################################
# IMPORTS ########################################################### IMPORTS #
###############################################################################

# Standard library
import logging
import functools
import pathlib
import hashlib


# Installed
import boto3
import botocore


import rich
from rich.progress import Progress, SpinnerColumn

# Own modules
from cli_code.dds_exceptions import ChecksumError


###############################################################################
# START LOGGING CONFIG ################################# START LOGGING CONFIG #
###############################################################################

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

###############################################################################
# DECORATORS ##################################################### DECORATORS #
###############################################################################


def checksum_verification_required(func):
    """
    Checks if the user has chosen additional checksum verification.
    If yes, performs the verification.
    """

    @functools.wraps(func)
    def verify_checksum(correct_checksum, *args, do_verify: bool = False, **kwargs):

        done, message = (False, "")
        try:
            # Execute function
            chunks = func(*args, **kwargs)

            # Generate checksum and verify if option chosen by user
            if do_verify:
                LOG.info("Verifying file integrity...")
                checksum = hashlib.sha256()
                try:
                    for chunk in chunks:
                        checksum.update(chunk)
                except ValueError as cs_err:  # TODO (ina): Find suitable exception
                    message = str(cs_err)
                    LOG.exception(message)
                else:
                    checksum_digest = checksum.hexdigest()

                    if checksum_digest != correct_checksum:
                        message = "Checksum verification failed. File compromised."
                        LOG.warning(message)
                    else:
                        done = True
                        LOG.info("File integrity verified.")

        except ChecksumError as err:  # TODO (ina): Find suitable exception
            message = str(err)
            LOG.exception(message)
        else:
            done = True
            LOG.info("Function %s successfully finished.", func.__name__)

        return done, message

    return verify_checksum


def verify_proceed(func):
    """Decorator for verifying that the file is not cancelled.
    Also cancels the upload of all non-started files if break-on-fail."""

    @functools.wraps(func)
    def wrapped(self, file, *args, **kwargs):

        # Check if keyboardinterrupt in dds
        if self.stop_doing:
            # TODO (ina): Add save to status here
            message = "KeyBoardInterrupt - cancelling file {file}"
            LOG.warning(message)
            return False  # Do not proceed

        # Return if file cancelled by another file
        if self.status[file]["cancel"]:
            message = f"File already cancelled, stopping file {file}"
            LOG.warning(message)
            return False

        # Mark as started
        self.status[file]["started"] = True
        LOG.info("File %s started %s", file, func.__name__)

        # Run function
        ok_to_proceed, message = func(self, file=file, *args, **kwargs)

        # Cancel file(s) if something failed
        if not ok_to_proceed:
            LOG.warning("%s failed: %s", func.__name__, message)
            self.status[file].update({"cancel": True, "message": message})
            if self.break_on_fail:
                message = (
                    f"Cancelling upload due to file '{file}'. "
                    "Break-on-fail specified in call."
                )
                LOG.info(message)

                _ = [
                    self.status[x].update({"cancel": True, "message": message})
                    for x in self.status
                    if not self.status[x]["cancel"]
                    and not self.status[x]["started"]
                    and x != file
                ]

        return ok_to_proceed

    return wrapped


def update_status(func):
    """Decorator for updating the status of files."""

    @functools.wraps(func)
    def wrapped(self, file, *args, **kwargs):

        # TODO (ina): add processing?
        if func.__name__ not in ["put", "add_file_db", "get", "update_db"]:
            raise Exception(
                f"The function {func.__name__} cannot be used with this decorator."
            )
        if func.__name__ not in self.status[file]:
            raise Exception(f"No status found for function {func.__name__}.")

        # Update status to started
        self.status[file][func.__name__].update({"started": True})
        LOG.info("File %s status updated to %s: started", file, func.__name__)

        # Run function
        ok_to_continue, message, *_ = func(self, file=file, *args, **kwargs)
        # ok_to_continue = False
        if not ok_to_continue:
            # Save info about which operation failed
            self.status[file]["failed_op"] = func.__name__
            LOG.warning("%s failed: %s", func.__name__, message)
        else:
            # Update status to done
            self.status[file][func.__name__].update({"done": True})
            LOG.info("File %s status updated to %s: done", file, func.__name__)

        return ok_to_continue, message

    return wrapped


def connect_cloud(func):
    """Connect to S3"""

    @functools.wraps(func)
    def init_resource(self, *args, **kwargs):

        # Connect to service
        try:
            session = boto3.session.Session()

            self.resource = session.resource(
                service_name="s3",
                endpoint_url=self.url,
                aws_access_key_id=self.keys["access_key"],
                aws_secret_access_key=self.keys["secret_key"],
            )
        except (boto3.exceptions.Boto3Error, botocore.exceptions.BotoCoreError) as err:
            self.url, self.keys, self.message = (
                None,
                None,
                f"S3 connection failed: {err}",
            )
        else:
            LOG.info("Connection to S3 established.")
            return func(self, *args, **kwargs)

    return init_resource


def subpath_required(func):
    """Make sure that the subpath to the temporary file directory exist."""

    @functools.wraps(func)
    def check_and_create(self, file, *args, **kwargs):
        """Create the sub directory if it does not exist."""

        file_info = self.filehandler.data[file]

        # Required path
        full_subpath = self.filehandler.local_destination / pathlib.Path(
            file_info["subpath"]
        )

        # Create path
        if not full_subpath.exists():
            try:
                full_subpath.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                return False, str(err)

            LOG.info("New directory created: %s", full_subpath)

        return func(self, file=file, *args, **kwargs)

    return check_and_create


def removal_spinner(func):
    """Spinner for the rm command"""

    @functools.wraps(func)
    def create_and_remove_task(self, *args, **kwargs):

        message = ""

        with Progress(
            "[bold]{task.description}",
            SpinnerColumn(spinner_name="dots12", style="white"),
        ) as progress:

            # Determine spinner text
            if func.__name__ == "remove_all":
                description = f"Removing all files in project {self.project}..."
            elif func.__name__ == "remove_file":
                description = "Removing file(s)..."
            elif func.__name__ == "remove_folder":
                description = "Removing folder(s)..."

            # Add progress task
            task = progress.add_task(description=description)

            # Execute function
            message = func(self, *args, **kwargs)

            # Remove progress task
            progress.remove_task(task)

        # Printout removal response
        console = rich.console.Console()
        console.print(message)

    return create_and_remove_task
