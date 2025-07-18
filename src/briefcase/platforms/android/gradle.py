from __future__ import annotations

import contextlib
import datetime
import re
import subprocess
import time
from pathlib import Path

from briefcase.commands import (
    BuildCommand,
    CreateCommand,
    OpenCommand,
    PackageCommand,
    PublishCommand,
    RunCommand,
    UpdateCommand,
)
from briefcase.config import AppConfig, parsed_version
from briefcase.console import ANSI_ESC_SEQ_RE_DEF
from briefcase.exceptions import BriefcaseCommandError
from briefcase.integrations.android_sdk import ADB, AndroidSDK
from briefcase.integrations.subprocess import SubprocessArgT


def safe_formal_name(name):
    """Converts the name into a safe name on Android.

    Certain characters (``/\\:<>"?*|``) can't be used as app names
    on Android; ``!`` causes problems with Android build tooling.
    Also ensure that trailing, leading, and consecutive whitespace
    caused by removing punctuation is collapsed.

    :param name: The candidate name
    :returns: The safe version of the name.
    """
    return re.sub(r"\s+", " ", re.sub(r'[!/\\:<>"\?\*\|]', "", name)).strip()


# Matches zero or more ANSI control chars wrapping the message for when
# the Android emulator is printing in color.
ANDROID_LOG_PREFIX_REGEX = re.compile(
    rf"(?:{ANSI_ESC_SEQ_RE_DEF})*[A-Z]/(?P<tag>.*?): (?P<content>.*?(?=\x1B|$))(?:{ANSI_ESC_SEQ_RE_DEF})*"
)


def android_log_clean_filter(line):
    """Filter an ADB log to extract the Python-generated message content.

    Any system or stub messages are ignored; all logging prefixes are stripped.
    Python code is identified as coming from the ``python.stdout``

    :param line: The raw line from the system log
    :returns: A tuple, containing (a) the log line, stripped of any system
        logging context, and (b) a boolean indicating if the message should be
        included for analysis purposes (i.e., it's Python content, not a system
        message).
    """
    match = ANDROID_LOG_PREFIX_REGEX.match(line)
    if match:
        groups = match.groupdict()
        include = groups["tag"] in {"python.stdout", "python.stderr"}
        return groups["content"], include

    return line, False


class GradleMixin:
    output_format = "gradle"
    platform = "android"
    platform_target_version = "0.3.15"

    @property
    def packaging_formats(self):
        return ["aab", "apk", "debug-apk"]

    @property
    def default_packaging_format(self):
        return "aab"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def project_path(self, app):
        return self.bundle_path(app)

    def binary_path(self, app):
        return (
            self.bundle_path(app)
            / "app"
            / "build"
            / "outputs"
            / "apk"
            / "debug"
            / "app-debug.apk"
        )

    def distribution_path(self, app):
        extension = {
            "aab": "aab",
            "apk": "apk",
            "debug-apk": "debug.apk",
        }[app.packaging_format]
        return self.dist_path / f"{app.formal_name}-{app.version}.{extension}"

    def run_gradle(self, app, args: list[SubprocessArgT]):
        # Gradle may install the emulator via the dependency chain build-tools > tools >
        # emulator. (The `tools` package only shows up in sdkmanager if you pass
        # `--include_obsolete`.) However, the old sdkmanager built into Android Gradle
        # plugin 4.2 doesn't know about macOS on ARM, so it'll install an x86_64 emulator
        # which won't work with ARM system images.
        #
        # Work around this by pre-installing the emulator with our own sdkmanager before
        # running Gradle. For simplicity, we do this on all platforms, since the user will
        # almost certainly want an emulator soon enough.
        self.tools.android_sdk.verify_emulator()

        gradlew = "gradlew.bat" if self.tools.host_os == "Windows" else "gradlew"
        self.tools.subprocess.run(
            # Windows needs the full path to `gradlew`; macOS & Linux can find it
            # via `./gradlew`. For simplicity of implementation, we always provide
            # the full path.
            [
                self.bundle_path(app) / gradlew,
                "--console",
                "plain",
            ]
            + (["--debug"] if self.tools.console.is_deep_debug else [])
            + args,
            env=self.tools.android_sdk.env,
            # Set working directory so gradle can use the app bundle path as its
            # project root, i.e., to avoid 'Task assembleDebug not found'.
            cwd=self.bundle_path(app),
            check=True,
            # Gradle writes to stdout using the system encoding. So, explicitly use it
            # here to avoid defaulting to the console encoding for the subprocess call.
            # This is mostly for the benefit of Windows where the system encoding may
            # not be the same as the console encoding and typically neither are UTF-8.
            # See #1425 for details.
            encoding=self.tools.system_encoding,
        )

    def verify_tools(self):
        """Verify that the Android APK tools in `briefcase` will operate on this system,
        downloading tools as needed."""
        super().verify_tools()
        AndroidSDK.verify(tools=self.tools)
        if not self.is_clone:
            self.console.add_log_file_extra(self.tools.android_sdk.list_packages)


class GradleCreateCommand(GradleMixin, CreateCommand):
    description = "Create and populate an Android Gradle project."
    hidden_app_properties = {"permission", "feature"}

    def support_package_filename(self, support_revision):
        """The query arguments to use in a support package query request."""
        return (
            f"Python-{self.python_version_tag}-Android-support.b{support_revision}.zip"
        )

    def output_format_template_context(self, app: AppConfig):
        """Additional template context required by the output format.

        :param app: The config object for the app
        """
        # Android requires an integer "version code". If a version code
        # isn't explicitly provided, generate one from the version number.
        # The build number will also be appended, if provided.
        try:
            version_code = app.version_code
        except AttributeError:
            parsed = parsed_version(app.version)

            v = (list(parsed.release) + [0, 0])[:3]  # version triple
            build = int(getattr(app, "build", "0"))
            version_code = f"{v[0]:d}{v[1]:02d}{v[2]:02d}{build:02d}".lstrip("0")

        # The default runtime libraries included in an app. The default value is the
        # list that was hard-coded in the Briefcase 0.3.16 Android template, prior to
        # the introduction of customizable system requirements for Android.
        try:
            dependencies = app.build_gradle_dependencies
        except AttributeError:
            self.console.warning(
                """
*************************************************************************
** WARNING: App does not define build_gradle_dependencies              **
*************************************************************************

    The Android configuration for this app does not contain a
    `build_gradle_dependencies` definition. Briefcase will use a default
    value of:

        build_gradle_dependencies = [
            "androidx.appcompat:appcompat:1.0.2",
            "androidx.constraintlayout:constraintlayout:1.1.3",
            "androidx.swiperefreshlayout:swiperefreshlayout:1.1.0",
        ]

    You should add this definition to the Android configuration
    of your project's pyproject.toml file. See:

        https://briefcase.readthedocs.io/en/stable/reference/platforms/android/gradle.html#build-gradle-dependencies

    for more information.

*************************************************************************

"""
            )
            dependencies = [
                "androidx.appcompat:appcompat:1.0.2",
                "androidx.constraintlayout:constraintlayout:1.1.3",
                "androidx.swiperefreshlayout:swiperefreshlayout:1.1.0",
            ]

        return {
            "version_code": version_code,
            "safe_formal_name": safe_formal_name(app.formal_name),
            # Extract test packages, to enable features like test discovery and assertion
            # rewriting.
            "extract_packages": ", ".join(
                f'"{name}"'
                for path in (app.test_sources or [])
                if (name := Path(path).name)
            ),
            "build_gradle_dependencies": {"implementation": dependencies},
        }

    def permissions_context(self, app: AppConfig, x_permissions: dict[str, str]):
        """Additional template context for permissions.

        :param app: The config object for the app
        :param x_permissions: The dictionary of known cross-platform permission
            definitions.
        :returns: The template context describing permissions for the app.
        """
        # Default permissions for all Android apps
        permissions = {
            "android.permission.INTERNET": True,
            "android.permission.ACCESS_NETWORK_STATE": True,
        }

        # Default feature usage for all Android apps
        features = {}

        if x_permissions["camera"]:
            permissions["android.permission.CAMERA"] = True
            features["android.hardware.camera"] = False
            features["android.hardware.camera.any"] = False
            features["android.hardware.camera.front"] = False
            features["android.hardware.camera.external"] = False
            features["android.hardware.camera.autofocus"] = False

        if x_permissions["microphone"]:
            permissions["android.permission.RECORD_AUDIO"] = True

        if x_permissions["fine_location"]:
            permissions["android.permission.ACCESS_FINE_LOCATION"] = True
            features["android.hardware.location.network"] = False
            features["android.hardware.location.gps"] = False

        if x_permissions["coarse_location"]:
            permissions["android.permission.ACCESS_COARSE_LOCATION"] = True
            features["android.hardware.location.network"] = False
            features["android.hardware.location.gps"] = False

        if x_permissions["background_location"]:
            permissions["android.permission.ACCESS_BACKGROUND_LOCATION"] = True
            features["android.hardware.location.network"] = False
            features["android.hardware.location.gps"] = False

        if x_permissions["photo_library"]:
            permissions["android.permission.READ_MEDIA_VISUAL_USER_SELECTED"] = True

        # Override any permission and entitlement definitions with the platform specific definitions
        permissions.update(app.permission)
        features.update(getattr(app, "feature", {}))

        return {
            "permissions": permissions,
            "features": features,
        }


class GradleUpdateCommand(GradleCreateCommand, UpdateCommand):
    description = "Update an existing Android Gradle project."


class GradleOpenCommand(GradleMixin, OpenCommand):
    description = "Open the folder for an existing Android Gradle project."


class GradleBuildCommand(GradleMixin, BuildCommand):
    description = "Build an Android debug APK."

    def metadata_resource_path(self, app: AppConfig):
        return self.bundle_path(app) / self.path_index(app, "metadata_resource_path")

    def update_app_metadata(self, app: AppConfig):
        with self.console.wait_bar("Setting main module..."):
            with self.metadata_resource_path(app).open("w", encoding="utf-8") as f:
                # Set the name of the app's main module; this will depend
                # on whether we're in test mode.
                f.write(
                    f"""\
<resources>
    <string name="main_module">{app.main_module()}</string>
</resources>
"""
                )

    def build_app(self, app: AppConfig, **kwargs):
        """Build an application.

        :param app: The application to build
        """
        self.console.info("Updating app metadata...", prefix=app.app_name)
        self.update_app_metadata(app=app)

        self.console.info("Building Android APK...", prefix=app.app_name)
        with self.console.wait_bar("Building..."):
            try:
                self.run_gradle(app, ["assembleDebug"])
            except subprocess.CalledProcessError as e:
                raise BriefcaseCommandError("Error while building project.") from e


class GradleRunCommand(GradleMixin, RunCommand):
    description = "Run an Android debug APK on a device (physical or virtual)."

    def verify_tools(self):
        super().verify_tools()
        self.tools.android_sdk.verify_emulator()

    def add_options(self, parser):
        super().add_options(parser)
        parser.add_argument(
            "-d",
            "--device",
            dest="device_or_avd",
            help=(
                "The device to target; either a device ID for a physical device, "
                " or an AVD name ('@emulatorName') "
            ),
            required=False,
        )
        parser.add_argument(
            "--Xemulator",
            action="append",
            dest="extra_emulator_args",
            help="Additional arguments to use when starting the emulator",
            required=False,
        )
        parser.add_argument(
            "--shutdown-on-exit",
            action="store_true",
            help="Shutdown the emulator on exit",
            required=False,
        )
        parser.add_argument(
            "--forward-port",
            action="append",
            dest="forward_ports",
            type=int,
            help="Forward the specified port from host to device.",
        )
        parser.add_argument(
            "--reverse-port",
            action="append",
            dest="reverse_ports",
            type=int,
            help="Reverse the specified port from device to host.",
        )

    def run_app(
        self,
        app: AppConfig,
        passthrough: list[str],
        device_or_avd=None,
        extra_emulator_args=None,
        shutdown_on_exit=False,
        forward_ports: list[int] | None = None,
        reverse_ports: list[int] | None = None,
        **kwargs,
    ):
        """Start the application.

        :param app: The config object for the app
        :param passthrough: The list of arguments to pass to the app
        :param device_or_avd: The device to target. If ``None``, the user will
            be asked to re-run the command selecting a specific device.
        :param extra_emulator_args: Any additional arguments to pass to the emulator.
        :param shutdown_on_exit: Should the emulator be shut down on exit?
        :param forward_ports: A list of ports to forward for the app.
        :param reverse_ports: A list of ports to reversed for the app.
        """
        device, name, avd = self.tools.android_sdk.select_target_device(device_or_avd)

        # If there's no device ID, that means the emulator isn't running.
        # If there's no AVD either, it means the user has chosen to create
        # an entirely new emulator. Create the emulator (if necessary),
        # then start it.
        if device is None:
            if avd is None:
                avd = self.tools.android_sdk.create_emulator()
            else:
                # Ensure the system image for the requested emulator is available.
                # This step is only needed if the AVD already existed; you have to
                # have an image available to create an AVD.
                self.tools.android_sdk.verify_avd(avd)

            if extra_emulator_args:
                extra = f" (with {' '.join(extra_emulator_args)})"
            else:
                extra = ""
            self.console.info(f"Starting emulator {avd}{extra}...", prefix=app.app_name)
            device, name = self.tools.android_sdk.start_emulator(
                avd, extra_emulator_args
            )

        try:
            label = "test suite" if app.test_mode else "app"

            self.console.info(
                f"Starting {label} on {name} (device ID {device})", prefix=app.app_name
            )

            # Create an ADB wrapper for the selected device
            adb = self.tools.android_sdk.adb(device=device)

            # Compute Android package name. The Android template uses
            # `package_name` and `module_name`, so we use those here as well.
            package = f"{app.package_name}.{app.module_name}"

            # We force-stop the app to ensure the activity launches freshly.
            self.console.info("Installing app...", prefix=app.app_name)
            with self.console.wait_bar("Stopping old versions of the app..."):
                adb.force_stop_app(package)

            # Install the latest APK file onto the device.
            with self.console.wait_bar("Installing new app version..."):
                adb.install_apk(self.binary_path(app))

            forward_ports = forward_ports or []
            reverse_ports = reverse_ports or []

            # Forward/Reverse requested ports
            with self.forward_ports(adb, forward_ports, reverse_ports):
                # To start the app, we launch `org.beeware.android.MainActivity`.
                with self.console.wait_bar(f"Launching {label}..."):
                    # capture the earliest time for device logging in case PID not found
                    device_start_time = adb.datetime()

                    adb.start_app(
                        package, "org.beeware.android.MainActivity", passthrough
                    )

                    # Try to get the PID for 5 seconds.
                    pid = None
                    fail_time = datetime.datetime.now() + datetime.timedelta(seconds=5)
                    while not pid and datetime.datetime.now() < fail_time:
                        # Try to get the PID; run in quiet mode because we may
                        # need to do this a lot in the next 5 seconds.
                        pid = adb.pidof(package, quiet=2)
                        if not pid:
                            time.sleep(0.01)

                if pid:
                    self.console.info(
                        "Following device log output (type CTRL-C to stop log)...",
                        prefix=app.app_name,
                    )
                    # Start adb's logcat in a way that lets us stream the logs
                    log_popen = adb.logcat(pid=pid)

                    # Stream the app logs.
                    self._stream_app_logs(
                        app,
                        popen=log_popen,
                        clean_filter=android_log_clean_filter,
                        clean_output=False,
                        # Check for the PID in quiet mode so logs aren't corrupted.
                        stop_func=lambda: not adb.pid_exists(pid=pid, quiet=2),
                        log_stream=True,
                    )
                else:
                    self.console.error(
                        "Unable to find PID for app", prefix=app.app_name
                    )
                    self.console.error("Logs for launch attempt follow...")
                    self.console.error("=" * 75)

                    # Show the log from the start time of the app
                    adb.logcat_tail(since=device_start_time)

                    raise BriefcaseCommandError(
                        f"Problem starting app {app.app_name!r}"
                    )

        finally:
            if shutdown_on_exit:
                with self.tools.console.wait_bar("Stopping emulator..."):
                    adb.kill()

    @contextlib.contextmanager
    def forward_ports(
        self, adb: ADB, forward_ports: list[int], reverse_ports: list[int]
    ):
        """Establish a port forwarding/reversion.

        :param adb: The ADB wrapper for the device
        :param forward_ports: Ports to forward via ADB
        :param reverse_ports: Ports to reverse via ADB
        """
        for port in forward_ports:
            adb.forward(port, port)
        for port in reverse_ports:
            adb.reverse(port, port)

        yield

        for port in forward_ports:
            adb.forward_remove(port)
        for port in reverse_ports:
            adb.reverse_remove(port)


class GradlePackageCommand(GradleMixin, PackageCommand):
    description = "Create an Android App Bundle and APK in release mode."

    def package_app(self, app: AppConfig, **kwargs):
        """Package the app for distribution.

        This involves building the release app bundle.

        :param app: The application to build
        """
        self.console.info(
            "Building Android App Bundle and APK in release mode...",
            prefix=app.app_name,
        )
        with self.console.wait_bar("Bundling..."):
            build_type, build_artefact_path = {
                "aab": ("bundleRelease", "bundle/release/app-release.aab"),
                "apk": ("assembleRelease", "apk/release/app-release-unsigned.apk"),
                "debug-apk": ("assembleDebug", "apk/debug/app-debug.apk"),
            }[app.packaging_format]

            try:
                self.run_gradle(app, [build_type])
            except subprocess.CalledProcessError as e:
                raise BriefcaseCommandError("Error while building project.") from e

        # Move artefact to final location.
        self.tools.shutil.move(
            self.bundle_path(app) / "app/build/outputs" / build_artefact_path,
            self.distribution_path(app),
        )


class GradlePublishCommand(GradleMixin, PublishCommand):
    description = "Publish an Android APK."


# Declare the briefcase command bindings
create = GradleCreateCommand
open = GradleOpenCommand
update = GradleUpdateCommand
build = GradleBuildCommand
run = GradleRunCommand
package = GradlePackageCommand
publish = GradlePublishCommand
