#!/usr/bin/env -S python -u
#
# Copyright (C) 2022 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Analyze bootclasspath_fragment usage."""
import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import typing
import sys

_STUB_FLAGS_FILE = "out/soong/hiddenapi/hiddenapi-stub-flags.txt"

_FLAGS_FILE = "out/soong/hiddenapi/hiddenapi-flags.csv"

_INCONSISTENT_FLAGS = "ERROR: Hidden API flags are inconsistent:"


class BuildOperation:

    def __init__(self, popen):
        self.popen = popen
        self.returncode = None

    def lines(self):
        """Return an iterator over the lines output by the build operation.

        The lines have had any trailing white space, including the newline
        stripped.
        """
        return newline_stripping_iter(self.popen.stdout.readline)

    def wait(self, *args, **kwargs):
        self.popen.wait(*args, **kwargs)
        self.returncode = self.popen.returncode


@dataclasses.dataclass()
class FlagDiffs:
    """Encapsulates differences in flags reported by the build"""

    # Map from member signature to the (module flags, monolithic flags)
    diffs: typing.Dict[str, typing.Tuple[str, str]]


@dataclasses.dataclass()
class ModuleInfo:
    """Provides access to the generated module-info.json file.

    This is used to find the location of the file within which specific modules
    are defined.
    """

    modules: typing.Dict[str, typing.Dict[str, typing.Any]]

    @staticmethod
    def load(filename):
        with open(filename, "r", encoding="utf8") as f:
            j = json.load(f)
            return ModuleInfo(j)

    def _module(self, module_name):
        """Find module by name in module-info.json file"""
        if module_name in self.modules:
            return self.modules[module_name]

        raise Exception(f"Module {module_name} could not be found")

    def module_path(self, module_name):
        module = self._module(module_name)
        # The "path" is actually a list of paths, one for each class of module
        # but as the modules are all created from bp files if a module does
        # create multiple classes of make modules they should all have the same
        # path.
        paths = module["path"]
        unique_paths = set(paths)
        if len(unique_paths) != 1:
            raise Exception(f"Expected module '{module_name}' to have a "
                            f"single unique path but found {unique_paths}")
        return paths[0]


@dataclasses.dataclass
class FileChange:
    path: str

    description: str

    def __lt__(self, other):
        return self.path < other.path


@dataclasses.dataclass
class HiddenApiPropertyChange:

    property_name: str

    values: typing.List[str]

    property_comment: str = ""

    def snippet(self, indent):
        snippet = "\n"
        snippet += format_comment_as_text(self.property_comment, indent)
        snippet += f"{indent}{self.property_name}: ["
        if self.values:
            snippet += "\n"
            for value in self.values:
                snippet += f'{indent}    "{value}",\n'
            snippet += f"{indent}"
        snippet += "],\n"
        return snippet


@dataclasses.dataclass()
class Result:
    """Encapsulates the result of the analysis."""

    # The diffs in the flags.
    diffs: typing.Optional[FlagDiffs] = None

    # The bootclasspath_fragment hidden API properties changes.
    property_changes: typing.List[HiddenApiPropertyChange] = dataclasses.field(
        default_factory=list)

    # The list of file changes.
    file_changes: typing.List[FileChange] = dataclasses.field(
        default_factory=list)


@dataclasses.dataclass()
class BcpfAnalyzer:
    # Directory pointed to by ANDROID_BUILD_OUT
    top_dir: str

    # Directory pointed to by OUT_DIR of {top_dir}/out if that is not set.
    out_dir: str

    # Directory pointed to by ANDROID_PRODUCT_OUT.
    product_out_dir: str

    # The name of the bootclasspath_fragment module.
    bcpf: str

    # The name of the apex module containing {bcpf}, only used for
    # informational purposes.
    apex: str

    # The name of the sdk module containing {bcpf}, only used for
    # informational purposes.
    sdk: str

    # All the signatures, loaded from all-flags.csv, initialized by
    # load_all_flags().
    _signatures: typing.Set[str] = dataclasses.field(default_factory=set)

    # All the classes, loaded from all-flags.csv, initialized by
    # load_all_flags().
    _classes: typing.Set[str] = dataclasses.field(default_factory=set)

    # Information loaded from module-info.json, initialized by
    # load_module_info().
    module_info: ModuleInfo = None

    @staticmethod
    def reformat_report_test(text):
        return re.sub(r"(.)\n([^\s])", r"\1 \2", text)

    def report(self, text, **kwargs):
        # Concatenate lines that are not separated by a blank line together to
        # eliminate formatting applied to the supplied text to adhere to python
        # line length limitations.
        text = self.reformat_report_test(text)
        logging.info("%s", text, **kwargs)

    def run_command(self, cmd, *args, **kwargs):
        cmd_line = " ".join(cmd)
        logging.debug("Running %s", cmd_line)
        subprocess.run(
            cmd,
            *args,
            check=True,
            cwd=self.top_dir,
            stderr=subprocess.STDOUT,
            stdout=log_stream_for_subprocess(),
            text=True,
            **kwargs)

    @property
    def signatures(self):
        if not self._signatures:
            raise Exception("signatures has not been initialized")
        return self._signatures

    @property
    def classes(self):
        if not self._classes:
            raise Exception("classes has not been initialized")
        return self._classes

    def load_all_flags(self):
        all_flags = self.find_bootclasspath_fragment_output_file(
            "all-flags.csv")

        # Extract the set of signatures and a separate set of classes produced
        # by the bootclasspath_fragment.
        with open(all_flags, "r", encoding="utf8") as f:
            for line in newline_stripping_iter(f.readline):
                signature = self.line_to_signature(line)
                self._signatures.add(signature)
                class_name = self.signature_to_class(signature)
                self._classes.add(class_name)

    def load_module_info(self):
        module_info_file = os.path.join(self.product_out_dir,
                                        "module-info.json")
        self.report(f"""
Making sure that {module_info_file} is up to date.
""")
        output = self.build_file_read_output(module_info_file)
        lines = output.lines()
        for line in lines:
            logging.debug("%s", line)
        output.wait(timeout=10)
        if output.returncode:
            raise Exception(f"Error building {module_info_file}")
        abs_module_info_file = os.path.join(self.top_dir, module_info_file)
        self.module_info = ModuleInfo.load(abs_module_info_file)

    @staticmethod
    def line_to_signature(line):
        return line.split(",")[0]

    @staticmethod
    def signature_to_class(signature):
        return signature.split(";->")[0]

    @staticmethod
    def to_parent_package(pkg_or_class):
        return pkg_or_class.rsplit("/", 1)[0]

    def module_path(self, module_name):
        return self.module_info.module_path(module_name)

    def module_out_dir(self, module_name):
        module_path = self.module_path(module_name)
        return os.path.join(self.out_dir, "soong/.intermediates", module_path,
                            module_name)

    def find_bootclasspath_fragment_output_file(self, basename):
        # Find the output file of the bootclasspath_fragment with the specified
        # base name.
        found_file = ""
        bcpf_out_dir = self.module_out_dir(self.bcpf)
        for (dirpath, _, filenames) in os.walk(bcpf_out_dir):
            for f in filenames:
                if f == basename:
                    found_file = os.path.join(dirpath, f)
                    break
        if not found_file:
            raise Exception(f"Could not find {basename} in {bcpf_out_dir}")
        return found_file

    def analyze(self):
        """Analyze a bootclasspath_fragment module.

        Provides help in resolving any existing issues and provides
        optimizations that can be applied.
        """
        self.report(f"Analyzing bootclasspath_fragment module {self.bcpf}")
        self.report(f"""
Run this tool to help initialize a bootclasspath_fragment module. Before you
start make sure that:

1. The current checkout is up to date.

2. The environment has been initialized using lunch, e.g.
   lunch aosp_arm64-userdebug

3. You have added a bootclasspath_fragment module to the appropriate Android.bp
file. Something like this:

   bootclasspath_fragment {{
     name: "{self.bcpf}",
     contents: [
       "...",
     ],

     // The bootclasspath_fragments that provide APIs on which this depends.
     fragments: [
       {{
         apex: "com.android.art",
         module: "art-bootclasspath-fragment",
       }},
     ],
   }}

4. You have added it to the platform_bootclasspath module in
frameworks/base/boot/Android.bp. Something like this:

   platform_bootclasspath {{
     name: "platform-bootclasspath",
     fragments: [
       ...
       {{
         apex: "{self.apex}",
         module: "{self.bcpf}",
       }},
     ],
   }}

5. You have added an sdk module. Something like this:

   sdk {{
     name: "{self.sdk}",
     bootclasspath_fragments: ["{self.bcpf}"],
   }}
""")

        # Make sure that the module-info.json file is up to date.
        self.load_module_info()

        self.report("""
Cleaning potentially stale files.
""")
        # Remove the out/soong/hiddenapi files.
        shutil.rmtree(f"{self.out_dir}/soong/hiddenapi", ignore_errors=True)

        # Remove any bootclasspath_fragment output files.
        shutil.rmtree(self.module_out_dir(self.bcpf), ignore_errors=True)

        self.build_monolithic_stubs_flags()

        result = Result()

        self.build_monolithic_flags(result)

        # If there were any changes that need to be made to the Android.bp
        # file then report them.
        if result.property_changes:
            bcpf_dir = self.module_info.module_path(self.bcpf)
            bcpf_bp_file = os.path.join(self.top_dir, bcpf_dir, "Android.bp")
            hiddenapi_snippet = ""
            for property_change in result.property_changes:
                hiddenapi_snippet += property_change.snippet("        ")

            # Remove leading and trailing blank lines.
            hiddenapi_snippet = hiddenapi_snippet.strip("\n")

            result.file_changes.append(
                self.new_file_change(
                    bcpf_bp_file, f"""
Add the following snippet into the {self.bcpf} bootclasspath_fragment module
in the {bcpf_dir}/Android.bp file. If the hidden_api block already exists then
merge these properties into it.

    hidden_api: {{
{hiddenapi_snippet}
    }},
"""))

        if result.file_changes:
            self.report("""
The following modifications need to be made:""")
            result.file_changes.sort()
            for file_change in result.file_changes:
                self.report(f"""
    {file_change.path}
        {file_change.description}
""".lstrip("\n"))

    def new_file_change(self, file, description):
        return FileChange(
            path=os.path.relpath(file, self.top_dir), description=description)

    def check_inconsistent_flag_lines(self, significant, module_line,
                                      monolithic_line, separator_line):
        if not (module_line.startswith("< ") and
                monolithic_line.startswith("> ") and not separator_line):
            # Something went wrong.
            self.report(f"""Invalid build output detected:
  module_line: "{module_line}"
  monolithic_line: "{monolithic_line}"
  separator_line: "{separator_line}"
""")
            sys.exit(1)

        if significant:
            logging.debug("%s", module_line)
            logging.debug("%s", monolithic_line)
            logging.debug("%s", separator_line)

    def scan_inconsistent_flags_report(self, lines):
        """Scans a hidden API flags report

        The hidden API inconsistent flags report which looks something like
        this.

        < out/soong/.intermediates/.../filtered-stub-flags.csv
        > out/soong/hiddenapi/hiddenapi-stub-flags.txt

        < Landroid/compat/Compatibility;->clearOverrides()V
        > Landroid/compat/Compatibility;->clearOverrides()V,core-platform-api

        """

        # The basic format of an entry in the inconsistent flags report is:
        #   <module specific flag>
        #   <monolithic flag>
        #   <separator>
        #
        # Wrap the lines iterator in an iterator which returns a tuple
        # consisting of the three separate lines.
        triples = zip(lines, lines, lines)

        module_line, monolithic_line, separator_line = next(triples)
        significant = False
        bcpf_dir = self.module_info.module_path(self.bcpf)
        if os.path.join(bcpf_dir, self.bcpf) in module_line:
            # These errors are related to the bcpf being analyzed so
            # keep them.
            significant = True
        else:
            self.report(f"Filtering out errors related to {module_line}")

        self.check_inconsistent_flag_lines(significant, module_line,
                                           monolithic_line, separator_line)

        diffs = {}
        for module_line, monolithic_line, separator_line in triples:
            self.check_inconsistent_flag_lines(significant, module_line,
                                               monolithic_line, "")

            module_parts = module_line.removeprefix("< ").split(",")
            module_signature = module_parts[0]
            module_flags = module_parts[1:]

            monolithic_parts = monolithic_line.removeprefix("> ").split(",")
            monolithic_signature = monolithic_parts[0]
            monolithic_flags = monolithic_parts[1:]

            if module_signature != monolithic_signature:
                # Something went wrong.
                self.report(f"""Inconsistent signatures detected:
  module_signature: "{module_signature}"
  monolithic_signature: "{monolithic_signature}"
""")
                sys.exit(1)

            diffs[module_signature] = (module_flags, monolithic_flags)

            if separator_line:
                # If the separator line is not blank then it is the end of the
                # current report, and possibly the start of another.
                return separator_line, diffs

        return "", diffs

    def build_file_read_output(self, filename):
        # Make sure the filename is relative to top if possible as the build
        # may be using relative paths as the target.
        rel_filename = filename.removeprefix(self.top_dir)
        cmd = ["build/soong/soong_ui.bash", "--make-mode", rel_filename]
        cmd_line = " ".join(cmd)
        logging.debug("%s", cmd_line)
        # pylint: disable=consider-using-with
        output = subprocess.Popen(
            cmd,
            cwd=self.top_dir,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            text=True,
        )
        return BuildOperation(popen=output)

    def build_hiddenapi_flags(self, filename):
        output = self.build_file_read_output(filename)

        lines = output.lines()
        diffs = None
        for line in lines:
            logging.debug("%s", line)
            while line == _INCONSISTENT_FLAGS:
                line, diffs = self.scan_inconsistent_flags_report(lines)

        output.wait(timeout=10)
        if output.returncode != 0:
            logging.debug("Command failed with %s", output.returncode)
        else:
            logging.debug("Command succeeded")

        return diffs

    def build_monolithic_stubs_flags(self):
        self.report(f"""
Attempting to build {_STUB_FLAGS_FILE} to verify that the
bootclasspath_fragment has the correct API stubs available...
""")

        # Build the hiddenapi-stubs-flags.txt file.
        diffs = self.build_hiddenapi_flags(_STUB_FLAGS_FILE)
        if diffs:
            self.report(f"""
There is a discrepancy between the stub API derived flags created by the
bootclasspath_fragment and the platform_bootclasspath. See preceding error
messages to see which flags are inconsistent. The inconsistencies can occur for
a couple of reasons:

If you are building against prebuilts of the Android SDK, e.g. by using
TARGET_BUILD_APPS then the prebuilt versions of the APIs this
bootclasspath_fragment depends upon are out of date and need updating. See
go/update-prebuilts for help.

Otherwise, this is happening because there are some stub APIs that are either
provided by or used by the contents of the bootclasspath_fragment but which are
not available to it. There are 4 ways to handle this:

1. A java_sdk_library in the contents property will automatically make its stub
   APIs available to the bootclasspath_fragment so nothing needs to be done.

2. If the API provided by the bootclasspath_fragment is created by an api_only
   java_sdk_library (or a java_library that compiles files generated by a
   separate droidstubs module then it cannot be added to the contents and
   instead must be added to the api.stubs property, e.g.

   bootclasspath_fragment {{
     name: "{self.bcpf}",
     ...
     api: {{
       stubs: ["$MODULE-api-only"],"
     }},
   }}

3. If the contents use APIs provided by another bootclasspath_fragment then
   it needs to be added to the fragments property, e.g.

   bootclasspath_fragment {{
     name: "{self.bcpf}",
     ...
     // The bootclasspath_fragments that provide APIs on which this depends.
     fragments: [
       ...
       {{
         apex: "com.android.other",
         module: "com.android.other-bootclasspath-fragment",
       }},
     ],
   }}

4. If the contents use APIs from a module that is not part of another
   bootclasspath_fragment then it must be added to the additional_stubs
   property, e.g.

   bootclasspath_fragment {{
     name: "{self.bcpf}",
     ...
     additional_stubs: ["android-non-updatable"],
   }}

   Like the api.stubs property these are typically java_sdk_library modules but
   can be java_library too.

   Note: The "android-non-updatable" is treated as if it was a java_sdk_library
   which it is not at the moment but will be in future.
""")

        return diffs

    def build_monolithic_flags(self, result):
        self.report(f"""
Attempting to build {_FLAGS_FILE} to verify that the
bootclasspath_fragment has the correct hidden API flags...
""")

        # Build the hiddenapi-flags.csv file and extract any differences in
        # the flags between this bootclasspath_fragment and the monolithic
        # files.
        result.diffs = self.build_hiddenapi_flags(_FLAGS_FILE)

        # Load information from the bootclasspath_fragment's all-flags.csv file.
        self.load_all_flags()

        if result.diffs:
            self.report(f"""
There is a discrepancy between the hidden API flags created by the
bootclasspath_fragment and the platform_bootclasspath. See preceding error
messages to see which flags are inconsistent. The inconsistencies can occur for
a couple of reasons:

If you are building against prebuilts of this bootclasspath_fragment then the
prebuilt version of the sdk snapshot (specifically the hidden API flag files)
are inconsistent with the prebuilt version of the apex {self.apex}. Please
ensure that they are both updated from the same build.

1. There are custom hidden API flags specified in the one of the files in
   frameworks/base/boot/hiddenapi which apply to the bootclasspath_fragment but
   which are not supplied to the bootclasspath_fragment module.

2. The bootclasspath_fragment specifies invalid "package_prefixes" or
   "split_packages" properties that match packages and classes that it does not
   provide.

""")

            # Check to see if there are any hiddenapi related properties that
            # need to be added to the
            self.report("""
Checking custom hidden API flags....
""")
            self.check_frameworks_base_boot_hidden_api_files(result)

    def report_hidden_api_flag_file_changes(self, result, property_name,
                                            flags_file, rel_bcpf_flags_file,
                                            bcpf_flags_file):
        matched_signatures = set()
        # Open the flags file to read the flags from.
        with open(flags_file, "r", encoding="utf8") as f:
            for signature in newline_stripping_iter(f.readline):
                if signature in self.signatures:
                    # The signature is provided by the bootclasspath_fragment so
                    # it will need to be moved to the bootclasspath_fragment
                    # specific file.
                    matched_signatures.add(signature)

        # If the bootclasspath_fragment specific flags file is not empty
        # then it contains flags. That could either be new flags just moved
        # from frameworks/base or previous contents of the file. In either
        # case the file must not be removed.
        if matched_signatures:
            insert = textwrap.indent("\n".join(matched_signatures),
                                     "            ")
            result.file_changes.append(
                self.new_file_change(
                    flags_file, f"""Remove the following entries:
{insert}
"""))

            result.file_changes.append(
                self.new_file_change(
                    bcpf_flags_file, f"""Add the following entries:
{insert}
"""))

            result.property_changes.append(
                HiddenApiPropertyChange(
                    property_name=property_name,
                    values=[rel_bcpf_flags_file],
                ))

    def check_frameworks_base_boot_hidden_api_files(self, result):
        hiddenapi_dir = os.path.join(self.top_dir,
                                     "frameworks/base/boot/hiddenapi")
        for basename in sorted(os.listdir(hiddenapi_dir)):
            if not (basename.startswith("hiddenapi-") and
                    basename.endswith(".txt")):
                continue

            flags_file = os.path.join(hiddenapi_dir, basename)

            logging.debug("Checking %s for flags related to %s", flags_file,
                          self.bcpf)

            # Map the file name in frameworks/base/boot/hiddenapi into a
            # slightly more meaningful name for use by the
            # bootclasspath_fragment.
            if basename == "hiddenapi-max-target-o.txt":
                basename = "hiddenapi-max-target-o-low-priority.txt"
            elif basename == "hiddenapi-max-target-r-loprio.txt":
                basename = "hiddenapi-max-target-r-low-priority.txt"

            property_name = basename.removeprefix("hiddenapi-")
            property_name = property_name.removesuffix(".txt")
            property_name = property_name.replace("-", "_")

            rel_bcpf_flags_file = f"hiddenapi/{basename}"
            bcpf_dir = self.module_info.module_path(self.bcpf)
            bcpf_flags_file = os.path.join(self.top_dir, bcpf_dir,
                                           rel_bcpf_flags_file)

            self.report_hidden_api_flag_file_changes(result, property_name,
                                                     flags_file,
                                                     rel_bcpf_flags_file,
                                                     bcpf_flags_file)


def newline_stripping_iter(iterator):
    """Return an iterator over the iterator that strips trailing white space."""
    lines = iter(iterator, "")
    lines = (line.rstrip() for line in lines)
    return lines


def format_comment_as_text(text, indent):
    return "".join(
        [f"{line}\n" for line in format_comment_as_lines(text, indent)])


def format_comment_as_lines(text, indent):
    lines = textwrap.wrap(text.strip("\n"), width=77 - len(indent))
    lines = [f"{indent}// {line}" for line in lines]
    return lines


def log_stream_for_subprocess():
    stream = subprocess.DEVNULL
    for handler in logging.root.handlers:
        if handler.level == logging.DEBUG:
            if isinstance(handler, logging.StreamHandler):
                stream = handler.stream
    return stream


def main(argv):
    args_parser = argparse.ArgumentParser(
        description="Analyze a bootclasspath_fragment module.")
    args_parser.add_argument(
        "--bcpf",
        help="The bootclasspath_fragment module to analyze",
        required=True,
    )
    args_parser.add_argument(
        "--apex",
        help="The apex module to which the bootclasspath_fragment belongs. It "
        "is not strictly necessary at the moment but providing it will "
        "allow this script to give more useful messages and it may be"
        "required in future.",
        default="SPECIFY-APEX-OPTION")
    args_parser.add_argument(
        "--sdk",
        help="The sdk module to which the bootclasspath_fragment belongs. It "
        "is not strictly necessary at the moment but providing it will "
        "allow this script to give more useful messages and it may be"
        "required in future.",
        default="SPECIFY-SDK-OPTION")
    args = args_parser.parse_args(argv[1:])
    top_dir = os.environ["ANDROID_BUILD_TOP"] + "/"
    out_dir = os.environ.get("OUT_DIR", os.path.join(top_dir, "out"))
    product_out_dir = os.environ.get("ANDROID_PRODUCT_OUT", top_dir)
    # Make product_out_dir relative to the top so it can be used as part of a
    # build target.
    product_out_dir = product_out_dir.removeprefix(top_dir)
    log_fd, abs_log_file = tempfile.mkstemp(
        suffix="_analyze_bcpf.log", text=True)

    with os.fdopen(log_fd, "w") as log_file:
        # Set up debug logging to the log file.
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)-8s %(message)s",
            stream=log_file)

        # define a Handler which writes INFO messages or higher to the
        # sys.stdout with just the message.
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        # add the handler to the root logger
        logging.getLogger("").addHandler(console)

        print(f"Writing log to {abs_log_file}")
        try:
            analyzer = BcpfAnalyzer(
                top_dir=top_dir,
                out_dir=out_dir,
                product_out_dir=product_out_dir,
                bcpf=args.bcpf,
                apex=args.apex,
                sdk=args.sdk,
            )
            analyzer.analyze()
        finally:
            print(f"Log written to {abs_log_file}")


if __name__ == "__main__":
    main(sys.argv)