"""
BlockVision platform.

Modeled after the Etherscan platform to fetch verified sources and compile.
"""

import json
import logging
import os
import re
import urllib.request
import urllib.error
from json.decoder import JSONDecodeError
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

from crytic_compile.compilation_unit import CompilationUnit
from crytic_compile.compiler.compiler import CompilerVersion
from crytic_compile.platform import solc_standard_json
from crytic_compile.platform.abstract_platform import AbstractPlatform
from crytic_compile.platform.exceptions import InvalidCompilation
from crytic_compile.platform.types import Type
from crytic_compile.utils.naming import Filename

if TYPE_CHECKING:
    from crytic_compile import CryticCompile

LOGGER = logging.getLogger("CryticCompile")

# Prefer requests if available (some APIs are picky about User-Agent/headers)
try:  # pragma: no cover - optional dependency
    import requests  # type: ignore

    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    requests = None
    _HAS_REQUESTS = False


# Base API per docs: https://docs.blockvision.org/reference/retrieve-contract-source-code
# GET https://api.blockvision.org/v2/monad/contract/source/code?address=<addr>
BLOCKVISION_BASE_SOURCE = "https://api.blockvision.org/v2/%s/contract/source/code?address=%s"

# For explorer page scraping (bytecode fallback). Adjust hosts if/when needed.
BLOCKVISION_BASE_BYTECODE = "https://%s/address/%s#code"


# Supported networks for BlockVision. Keys are the prefixes accepted before the colon
# in the target string (e.g., "blockvision.testnet.monad:0x..."), and values define
# (network_slug_for_api, explorer_host_for_bytecode_scrape).
SUPPORTED_NETWORK: Dict[str, Tuple[str, str]] = {
    # Monad testnet example
    "blockvision.testnet.monad": ("monad", "testnet.monadscan.com"),
}


def _handle_bytecode(crytic_compile: "CryticCompile", target: str, result_b: bytes) -> None:
    """Parse the bytecode and populate CryticCompile info (simple scraper).

    This follows the same approach as the Etherscan platform.
    """

    begin = """Search Algorithm">\nSimilar Contracts</button>\n"""
    begin += """<div id="dividcode">\n<pre class='wordwrap' style='height: 15pc;'>0x"""
    result = result_b.decode("utf8")
    result = result[result.find(begin) + len(begin) :]
    bytecode = result[: result.find("<")]

    contract_name = f"Contract_{target}"
    contract_filename = Filename(absolute="", relative="", short="", used="")

    compilation_unit = CompilationUnit(crytic_compile, str(target))
    source_unit = compilation_unit.create_source_unit(contract_filename)
    source_unit.add_contract_name(contract_name)
    compilation_unit.filename_to_contracts[contract_filename].add(contract_name)
    source_unit.abis[contract_name] = {}
    source_unit.bytecodes_init[contract_name] = bytecode
    source_unit.bytecodes_runtime[contract_name] = ""
    source_unit.srcmaps_init[contract_name] = []
    source_unit.srcmaps_runtime[contract_name] = []

    compilation_unit.compiler_version = CompilerVersion(
        compiler="unknown", version="", optimized=False
    )

    crytic_compile.bytecode_only = True


def _handle_single_file(
    source_code: str, addr: str, prefix: Optional[str], contract_name: str, export_dir: str
) -> str:
    if prefix:
        filename = os.path.join(export_dir, f"{addr}{prefix}-{contract_name}.sol")
    else:
        filename = os.path.join(export_dir, f"{addr}-{contract_name}.sol")
    with open(filename, "w", encoding="utf8") as file_desc:
        file_desc.write(source_code)
    return filename


def _handle_multiple_files(
    dict_source_code: Dict, addr: str, prefix: Optional[str], contract_name: str, export_dir: str
) -> Tuple[List[str], str, Optional[List[str]]]:
    if prefix:
        directory = os.path.join(export_dir, f"{addr}{prefix}-{contract_name}")
    else:
        directory = os.path.join(export_dir, f"{addr}-{contract_name}")

    if "sources" in dict_source_code:
        source_codes = dict_source_code["sources"]
    else:
        source_codes = dict_source_code

    filtered_paths: List[str] = []
    for filename, source_code in source_codes.items():
        path_filename = PurePosixPath(filename)
        # Only keep Solidity/Vyper files
        if path_filename.suffix not in [".sol", ".vy"]:
            continue

        if "contracts" == path_filename.parts[0] and not filename.startswith("@"):
            path_filename = PurePosixPath(
                *path_filename.parts[path_filename.parts.index("contracts") :]
            )

        # Convert absolute paths into relative
        if path_filename.is_absolute():
            path_filename = PurePosixPath(*path_filename.parts[1:])

        filtered_paths.append(path_filename.as_posix())
        path_filename_disk = Path(directory, path_filename)

        allowed_path = os.path.abspath(directory)
        if os.path.commonpath((allowed_path, os.path.abspath(path_filename_disk))) != allowed_path:
            raise IOError(
                f"Path '{path_filename_disk}' is outside of the allowed directory: {allowed_path}"
            )
        if not os.path.exists(path_filename_disk.parent):
            os.makedirs(path_filename_disk.parent)
        with open(path_filename_disk, "w", encoding="utf8") as file_desc:
            file_desc.write(source_code["content"])

    remappings = dict_source_code.get("settings", {}).get("remappings", None)
    return list(filtered_paths), directory, _sanitize_remappings(remappings, directory)


class BlockVision(AbstractPlatform):
    """BlockVision platform"""

    NAME = "BlockVision"
    PROJECT_URL = "https://blockvision.org/"
    TYPE = Type.BLOCKVISION

    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    def compile(self, crytic_compile: "CryticCompile", **kwargs: str) -> None:
        target = self._target

        export_dir = kwargs.get("export_dir", "crytic-export")
        export_dir = os.path.join(export_dir, kwargs.get("blockvision_export_dir", "blockvision-contracts"))

        blockvision_api_key = kwargs.get("blockvision_api_key", None) or os.getenv("BLOCKVISION_API_KEY")

        # Select network mapping based on prefix like "blockvision.testnet.monad:0x..."
        if target.startswith(tuple(SUPPORTED_NETWORK)):
            prefix, addr = target.split(":", 1)
            network_slug, explorer_host = SUPPORTED_NETWORK[prefix]
            source_url = BLOCKVISION_BASE_SOURCE % (network_slug, addr)
            bytecode_url = BLOCKVISION_BASE_BYTECODE % (explorer_host, addr)
        else:
            # Fallback: assume monad slug if not prefixed, to avoid clashes with Etherscan
            addr = target
            prefix = None
            network_slug, explorer_host = ("monad", "testnet.monadscan.com")
            source_url = BLOCKVISION_BASE_SOURCE % (network_slug, addr)
            bytecode_url = BLOCKVISION_BASE_BYTECODE % (explorer_host, addr)

        only_source = kwargs.get("blockvision_only_source_code", False)
        only_bytecode = kwargs.get("blockvision_only_bytecode", False)

        common_headers = {
            "Accept": "application/json",
        }
        if blockvision_api_key:
            common_headers["x-api-key"] = blockvision_api_key
        # Align User-Agent with requests default to avoid WAF quirks
        common_headers["User-Agent"] = "python-requests/2.32.3"

        source_code: str = ""
        contract_name: str = ""
        result: Dict[str, Union[bool, str, int, Dict, List]] = {}

        if not only_bytecode:
            if _HAS_REQUESTS:
                # Primary path using requests (works in user's environment)
                try:
                    r = requests.get(source_url, headers=common_headers, timeout=25)
                    # r = requests.get(source_url, headers=common_headers, timeout=25)
                except Exception as e:  # pragma: no cover
                    raise InvalidCompilation(f"BlockVision network error: {e}") from e
                if r.status_code == 403:
                    # Retry with query param variants if forbidden
                    if blockvision_api_key:
                        for key_name in ("apikey", "apiKey", "x-api-key"):
                            retry_url = (
                                source_url
                                + ("&" if "?" in source_url else "?")
                                + f"{key_name}={blockvision_api_key}"
                            )
                            rr = requests.get(retry_url, headers=common_headers, timeout=25)
                            if rr.ok:
                                r = rr
                                break
                # Print raw API response for debugging/visibility
                # try:
                #     print(r.text)
                # except Exception:  # pragma: no cover
                #     try:
                #         print(r.content.decode("utf-8", errors="replace"))
                #     except Exception:
                #         pass

                if not r.ok:
                    raise InvalidCompilation(
                        f"BlockVision HTTP error {r.status_code}: {r.text}"
                    )
                payload = r.content
            else:
                # Fallback to urllib
                source_req = urllib.request.Request(source_url, headers=common_headers)
                def _do_request(req: urllib.request.Request) -> bytes:
                    with urllib.request.urlopen(req) as response:
                        return response.read()

                try:
                    payload = _do_request(source_req)
                except urllib.error.HTTPError as e:  # surface API error details and retry with query apikey
                    retry_payload = None
                    if blockvision_api_key and e.code in (401, 403):
                        for key_name in ("apikey", "apiKey", "x-api-key"):
                            retry_url = (
                                source_url + ("&" if "?" in source_url else "?") + f"{key_name}={blockvision_api_key}"
                            )
                            retry_req = urllib.request.Request(retry_url, headers=common_headers)
                            try:
                                retry_payload = _do_request(retry_req)
                                break
                            except Exception:  # pragma: no cover - continue variants
                                retry_payload = None
                    if retry_payload is None:
                        try:
                            body = e.read().decode("utf-8", errors="replace")
                        except Exception:  # pragma: no cover
                            body = ""
                        msg = f"BlockVision HTTP error {e.code}: {body or e.reason}"
                        LOGGER.error(msg)
                        raise InvalidCompilation(msg) from e
                    payload = retry_payload

                # Print raw API response for debugging/visibility
                # try:
                #     print(payload.decode("utf-8", errors="replace"))
                # except Exception:  # pragma: no cover
                #     pass

            try:
                info = json.loads(payload)
            except JSONDecodeError as exc:  # pragma: no cover - networking edge
                raise InvalidCompilation("Invalid BlockVision response") from exc

            # Per docs: { code, reason, message, result: { contractAddress, contractName, abi, creationByteCode, metadata, status, sourceCode: [ {name, content}, ... ] } }
            if not isinstance(info, dict) or "result" not in info:
                LOGGER.error("Incorrect BlockVision response")
                raise InvalidCompilation("Incorrect BlockVision response: " + source_url)

            if int(info.get("code", -1)) != 0 or str(info.get("message", "")) != "OK":
                # Map common auth errors
                if "Invalid API Key" in str(info.get("reason", "")):
                    LOGGER.error("Invalid BlockVision API Key")
                    raise InvalidCompilation("Invalid BlockVision API Key: " + source_url)
                LOGGER.error("BlockVision returned error: %s", info.get("reason", "unknown"))
                raise InvalidCompilation("BlockVision API error: " + str(info.get("reason", "unknown")))

            core = info.get("result", {}) or {}
            if not isinstance(core, dict):
                LOGGER.error("Unexpected BlockVision result schema")
                raise InvalidCompilation("Unexpected BlockVision result schema")

            # Capture values for compilation
            contract_name = str(core.get("contractName", ""))
            metadata_blob = core.get("metadata")
            if isinstance(metadata_blob, str):
                try:
                    metadata_json = json.loads(metadata_blob)
                except Exception:
                    metadata_json = {}
            elif isinstance(metadata_blob, dict):
                metadata_json = metadata_blob
            source_list = core.get("sourceCode") or []

            if not source_list:
                LOGGER.error("Contract has no public source code (BlockVision)")
                raise InvalidCompilation("Contract has no public source code: " + source_url)

            # Convert source array into the dict format expected by _handle_multiple_files
            # Preserve original folder structure using metadata.sources keys when available,
            # so relative imports (e.g., ../beacon/IBeacon.sol) resolve correctly.
            normalized_sources: Dict[str, Dict[str, str]] = {}
            path_hints: List[str] = []
            metadata_sources: Dict[str, Dict[str, str]] = {}
            if isinstance(metadata_json, dict) and isinstance(metadata_json.get("sources"), dict):
                metadata_sources = metadata_json.get("sources", {})  # type: ignore[assignment]
                path_hints = list(metadata_sources.keys())

            # Build basename->possible paths map from metadata, including content for disambiguation
            basename_to_paths: Dict[str, List[str]] = {}
            basename_to_path_and_content: Dict[str, List[Tuple[str, str]]] = {}
            for p in path_hints:
                base = str(PurePosixPath(p).name).lower()
                basename_to_paths.setdefault(base, []).append(p)
                try:
                    content_hint = str(metadata_sources.get(p, {}).get("content", ""))
                except Exception:
                    content_hint = ""
                basename_to_path_and_content.setdefault(base, []).append((p, content_hint))

            for f in source_list:
                try:
                    raw_name = str(f.get("name", "")).strip()
                    content = str(f.get("content", ""))
                except Exception:  # pragma: no cover - defensive
                    continue
                if not raw_name or not content:
                    continue

                # Prefer the exact path from metadata if we can infer it
                candidate_path: Optional[str] = None
                name_path = raw_name.lstrip("/")
                name_base = str(PurePosixPath(name_path).name).lower()

                # 1) Exact path match from metadata
                if name_path in path_hints:
                    candidate_path = name_path
                # 2) Disambiguate by matching content against metadata.sources
                if candidate_path is None and name_base in basename_to_path_and_content:
                    matches_by_content = [p for (p, ch) in basename_to_path_and_content[name_base] if ch == content]
                    if len(matches_by_content) == 1:
                        candidate_path = matches_by_content[0]
                # 3) Unique basename mapping
                if candidate_path is None and name_base in basename_to_paths:
                    candidates = [p for p in basename_to_paths[name_base] if p.endswith("/" + str(PurePosixPath(name_path).name))]
                    if len(candidates) == 1:
                        candidate_path = candidates[0]
                    elif len(basename_to_paths[name_base]) == 1:
                        candidate_path = basename_to_paths[name_base][0]

                # Fallbacks: if incoming name already includes folders, keep them; else tuck under "contracts/"
                if not candidate_path:
                    if "/" in name_path:
                        candidate_path = name_path
                    else:
                        # Try to find any hint that contains '/<name>' and keep that directory
                        hinted = next((p for p in path_hints if p.endswith("/" + name_path)), None)
                        candidate_path = hinted or f"contracts/{name_path}"

                candidate_path = str(PurePosixPath(candidate_path))
                normalized_sources[candidate_path] = {"content": content}

            has_sources = bool(normalized_sources)

            # Prepare a synthetic dict_source_code compatible object including settings from metadata
            dict_source_code: Dict[str, Union[Dict, str]] = {"sources": normalized_sources}

            # If metadata is a JSON string, parse and lift settings
            via_ir_enabled = None
            evm_version: Optional[str] = None
            optimization_used: bool = False
            optimize_runs = None
            compiler_version = None

            if isinstance(metadata_blob, str):
                try:
                    metadata_json = json.loads(metadata_blob)
                except Exception:  # pragma: no cover - metadata may be large/opaque
                    metadata_json = {}
            elif isinstance(metadata_blob, dict):
                metadata_json = metadata_blob
            else:
                metadata_json = {}

            # Extract settings if present
            settings = metadata_json.get("settings", {}) if isinstance(metadata_json, dict) else {}
            if isinstance(settings, dict):
                # viaIR
                via_ir_enabled = settings.get("viaIR", None)
                # optimizer
                opt = settings.get("optimizer") or {}
                if isinstance(opt, dict):
                    optimization_used = bool(opt.get("enabled", False))
                    try:
                        optimize_runs = int(opt.get("runs")) if opt.get("runs") is not None else None
                    except Exception:
                        optimize_runs = None
                # evmVersion
                evm_version = settings.get("evmVersion") or None
                # remappings
                if settings.get("remappings"):
                    dict_source_code.setdefault("settings", {})
                    if isinstance(dict_source_code["settings"], dict):
                        dict_source_code["settings"]["remappings"] = settings.get("remappings")

            # compiler version
            comp = metadata_json.get("compiler") if isinstance(metadata_json, dict) else None
            if isinstance(comp, dict) and isinstance(comp.get("version"), str):
                m = re.findall(r"\d+\.\d+\.\d+", comp["version"]) or []
                compiler_version = m[0] if m else None
        if not has_sources and not only_source:
            LOGGER.info("Source code not available from BlockVision, try bytecode-only scrape")
            req = urllib.request.Request(bytecode_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as response:
                html = response.read()
            _handle_bytecode(crytic_compile, target, html)
            return

        if not has_sources:
            LOGGER.error("Contract has no public source code (BlockVision)")
            raise InvalidCompilation("Contract has no public source code: " + source_url)

        if not os.path.exists(export_dir):
            os.makedirs(export_dir)

        # Preserve values inferred from metadata above; we'll only fill missing ones from
        # Etherscan-style top-level fields if present in the BlockVision response.
        # Variables already declared earlier: compiler_version, evm_version, optimization_used, optimize_runs

        # Extract compiler settings (attempt Etherscan-style fields first)
        if isinstance(result, dict):
            if compiler_version is None and isinstance(result.get("CompilerVersion"), str):
                m = re.findall(r"\d+\.\d+\.\d+", str(result["CompilerVersion"]))
                if m:
                    compiler_version = m[0]
            if evm_version is None and isinstance(result.get("EVMVersion"), str):
                evm_version = result["EVMVersion"] if result["EVMVersion"] != "Default" else None
            if (not optimization_used) and ("OptimizationUsed" in result):
                optimization_used = str(result.get("OptimizationUsed", "0")) == "1"
            if optimize_runs is None and optimization_used and "Runs" in result:
                try:
                    optimize_runs = int(result["Runs"])  # type: ignore[arg-type]
                except Exception:  # pragma: no cover - defensive
                    optimize_runs = None
        # Using the normalized dict_source_code generated above from BlockVision response
        if 'dict_source_code' not in locals():  # safety in case of early return
            LOGGER.error("Internal error: missing normalized sources")
            raise InvalidCompilation("Internal error: missing normalized sources")
        filenames, working_dir, remappings = _handle_multiple_files(
            dict_source_code, addr, prefix, contract_name or f"Contract_{addr}", export_dir
        )

        compilation_unit = CompilationUnit(crytic_compile, contract_name or f"Contract_{addr}")
        compilation_unit.compiler_version = CompilerVersion(
            compiler=kwargs.get("solc", "solc"),
            version=(compiler_version or ""),
            optimized=optimization_used,
            optimize_runs=optimize_runs,
        )
        compilation_unit.compiler_version.look_for_installed_version()

        # Optional: implementation address (proxy) if BlockVision exposes it
        implementation = None
        impl_key = "Implementation"
        if isinstance(result, dict) and result.get("Proxy") == "1" and impl_key in result:
            implementation = str(result[impl_key])
            if target.startswith(tuple(SUPPORTED_NETWORK)):
                implementation = f"{target[:target.find(':')]}:{implementation}"
            compilation_unit.implementation_address = implementation

        # Try compiling
        solc_standard_json.standalone_compile(
            filenames,
            compilation_unit,
            working_dir=working_dir,
            remappings=remappings,
            evm_version=evm_version,
            via_ir=via_ir_enabled,
        )

        metadata_config = {
            "solc_remaps": remappings if remappings else {},
            "solc_solcs_select": compiler_version or "",
            "solc_args": " ".join(
                filter(
                    None,
                    [
                        "--via-ir" if via_ir_enabled else "",
                        "--optimize --optimize-runs " + str(optimize_runs) if optimize_runs else "",
                        "--evm-version " + evm_version if evm_version else "",
                    ],
                )
            ),
        }

        with open(
            os.path.join(working_dir if working_dir else export_dir, "crytic_compile.config.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(metadata_config, f)

    def clean(self, **_kwargs: str) -> None:  # pylint: disable=unused-argument
        pass

    @staticmethod
    def is_supported(target: str, **kwargs: str) -> bool:  # pylint: disable=unused-argument
        """Support targets like "blockvision.testnet.monad:0x..." only, to avoid
        conflicting with the Etherscan platform heuristics.
        """
        if target.startswith(tuple(SUPPORTED_NETWORK)):
            # Ensure suffix is a valid address-like string
            addr = target[target.find(":") + 1 :]
            return bool(re.match(r"^\s*0x[a-fA-F0-9]{40}\s*$", addr))
        return False

    def is_dependency(self, _path: str) -> bool:  # pylint: disable=unused-argument
        return False

    def _guessed_tests(self) -> List[str]:  # pragma: no cover - platform has no tests
        return []


def _sanitize_remappings(
    remappings: Optional[List[str]], allowed_directory: str
) -> Optional[List[str]]:
    if remappings is None:
        return remappings

    allowed_path = os.path.abspath(allowed_directory)

    remappings_clean: List[str] = []
    for r in remappings:
        split = r.split("=", 2)
        if len(split) != 2:
            LOGGER.warning("Invalid remapping %s", r)
            continue

        origin, dest = split[0], PurePosixPath(split[1])

        if dest.is_absolute():
            dest = PurePosixPath(*dest.parts[1:])

        dest_disk = Path(allowed_directory, dest)

        if os.path.commonpath((allowed_path, os.path.abspath(dest_disk))) != allowed_path:
            LOGGER.warning("Remapping %s=%s is potentially unsafe, skipping", origin, dest)
            continue

        remappings_clean.append(f"{origin}={str(dest / '_')[:-1]}")

    return remappings_clean


