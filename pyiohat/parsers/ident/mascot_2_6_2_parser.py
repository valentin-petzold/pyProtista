"""Engine parser."""
import multiprocessing as mp
import sys
from itertools import repeat

import numpy as np
import pandas as pd
import regex as re
from loguru import logger
from tqdm import tqdm

from pyiohat.parsers.ident_base_parser import IdentBaseParser
from pyiohat.utils import merge_and_join_dicts

mascot_custom_psm_regex = re.compile(
    r"(?:[-+0-9]+),(?P<exp_mass>[0-9\.]+),(?:[-0-9\.]+),(?P<n_matched_ions>[0-9]+),(?P<seq>[A-Z]+),(?:[0-9]+),(?P<opt_mod_string>[0-9]+),(?P<score>[.0-9]+),(?:[0-9]+),(?:.+subst;)(?P<subst>.+)"
)


def _get_single_spec_df(reference_dict, spectrum):
    """Primary method for reading and storing information from a single spectrum.

    Args:
        reference_dict (dict): dict with reference columns to be filled in
        spectrum (tuple): spectrum data

    Returns:
        (pd.DataFrame): dataframe for single spec id

    """
    spec_records = []
    spec_level_dict = reference_dict.copy()
    query, spec_level_info = spectrum[:2]

    spec_level_dict["spectrum_title"] = re.search(
        r"(?<=title=)(.+)", spec_level_info
    ).group()
    spec_level_dict["charge"] = re.search(r"(?<=charge=)(\d+)", spec_level_info).group()
    spec_level_dict["spectrum_id"] = re.search(
        r"(?<=scans=)(\d+)", spec_level_info
    ).group()
    spec_level_dict["retention_time_seconds"] = re.search(
        r"(?<=rtinseconds=)(\d+\.\d+)", spec_level_info
    ).group()

    # Iterate children
    for psm in spectrum[2]:
        psm_level_dict = spec_level_dict.copy()
        psm_level_info = re.search(mascot_custom_psm_regex, psm).groupdict()
        psm_level_dict["exp_mz"] = psm_level_info["exp_mass"]
        psm_level_dict["mascot:num_matched_ions"] = psm_level_info["n_matched_ions"]
        psm_level_dict["sequence"] = psm_level_info["seq"]
        psm_level_dict["modifications"] = psm_level_info["opt_mod_string"]
        psm_level_dict["mascot:score"] = psm_level_info["score"]
        psm_level_dict["subst"] = psm_level_info["subst"]

        spec_records.append(psm_level_dict)

    return pd.DataFrame(spec_records)


class Mascot_2_6_2_Parser(IdentBaseParser):
    """File parser for MSGF+."""

    def __init__(self, *args, **kwargs):
        """Initialize parser.

        Reads in data file and provides mappings.
        """
        super().__init__(*args, **kwargs)
        self.style = "mascot_style_1"

        self.section_data, self.spectrum_data = self._get_data_on_spectrum_level()
        self.mods = {
            "opt": dict(
                re.findall(r"delta([\d]+)=[\d.]+,(\S*)", self.section_data["masses"])
            ),
            "fix": dict(
                re.findall(
                    r"FixedMod[\d]+=[\d.]+,(\S*)\s\((\w)\)", self.section_data["masses"]
                )
            ),
        }

        self.reference_dict["search_engine"] = "mascot_" + re.search(
            r"(?<=version=).*", self.section_data["header"]
        ).group().replace(".", "_")
        self.reference_dict["mascot:score"] = pd.NA

    @classmethod
    def check_parser_compatibility(cls, file):
        """Assert compatibility between file and parser.

        Args:
            file (str): path to input file

        Returns:
            bool: True if parser and file are compatible

        """
        is_dat = file.as_posix().endswith(".dat")

        with open(file.as_posix()) as f:
            try:
                head = "".join([next(f) for _ in range(5)])
            except StopIteration:
                head = ""
        contains_engine = "Mascot" in head
        return is_dat and contains_engine

    def _get_data_on_spectrum_level(self):
        """Provide aggregated data on spectrum level."""
        with open(self.input_file) as f:
            file_str = f.read()

        file_section_pattern = re.compile(
            r"(?:Content-Type: application/x-Mascot; name=\")([\w+]*)"
        )
        section_split = re.split(file_section_pattern, file_str)[1:]
        section_data = {k: v for k, v in zip(section_split[::2], section_split[1::2])}

        # Filters for non empty data and only respective _subst metainfo
        filter_pattern = re.compile(r"q[\d]+_p[\d]+=(?!-1$).+|q[\d]+_p[\d]+_subst=.+")
        peptide_data = dict(
            [
                peptide.split("=")
                for peptide in section_data["peptides"].split("\n")[2:-2]
                if re.match(filter_pattern, peptide)
            ]
        )
        base_entries = {k: v for k, v in peptide_data.items() if "_subst" not in k}
        subst_entries = {
            k.rstrip("_subst"): v for k, v in peptide_data.items() if "_subst" in k
        }

        peptide_data = merge_and_join_dicts(
            [base_entries, subst_entries], delimiter=";subst;"
        )
        psm_info = pd.DataFrame(
            peptide_data.values(), index=peptide_data.keys(), columns=["info"]
        )
        psm_info.index = psm_info.index.str.replace("q", "query")
        psm_info.reset_index(inplace=True)
        psm_info.groupby("index")["info"].apply(list).to_dict()
        psm_info.loc[:, ["index", "psm"]] = (
            psm_info["index"].str.split("_", expand=True).values
        )
        psm_info = psm_info.groupby("index")["info"].apply(list).to_dict()

        spectrum_info = {k: v for k, v in section_data.items() if k in psm_info}
        spectrum_data = [
            (k, v_section, psm_info[k]) for k, v_section in spectrum_info.items()
        ]

        section_data = {k: v for k, v in section_data.items() if "query" not in k}

        return section_data, spectrum_data

    def _translate_opt_mods(self, raw_mod):
        """Replace internal modification nomenclature with formatted modification strings.

        Args:
            raw_mod (str): unformatted mod string

        Returns:
            formatted_mod_str (str): formatted mod string
        """
        formatted_mod_str = ";"
        for pos, mod_key in enumerate(raw_mod):
            if mod_key == "0":
                continue
            if mod_key in self.mods["opt"]:
                formatted_mod_str += f"{self.mods['opt'][mod_key]}:{pos};"
            else:
                logger.error(f"Modification {mod_key} could not be mapped.")
                raise KeyError

        return formatted_mod_str

    def _format_mods(self):
        """Convert mods to unified modstring.

        Operations are performed inplace.
        """
        fix_mods = None
        for name, aa in self.mods["fix"].items():
            fm_strings = (
                self.df["sequence"]
                .str.split(aa)
                .apply(
                    lambda l: ";".join(
                        [
                            name + ":" + ind
                            for ind in (
                                np.cumsum(list(map(len, l[:-1]))) + range(1, len(l))
                            ).astype(str)
                        ]
                    )
                )
            )
            if fix_mods is None:
                fix_mods = fm_strings
            else:
                fix_mods = fix_mods + ";" + fm_strings + ";"

        self.df.loc[:, "modifications"] = (
            self.df["modifications"].apply(self._translate_opt_mods).to_list()
        )
        if fix_mods is not None:
            self.df.loc[:, "modifications"] += fix_mods

        # Add substitutions
        if self.df["subst"].str.match(r"(\d+,\w,\w)").any():
            subst_df = pd.DataFrame(
                self.df["subst"].str.findall(r"(\d+,\w,\w)").tolist()
            )
            subst_df = (
                "Subst("
                + subst_df[0].str.split(",").str[1]
                + "):"
                + subst_df[0].str.split(",").str[0]
            ).fillna("")
            self.df.loc[:, "modifications"] += subst_df

        self.df.drop(columns="subst", inplace=True)

    def unify(self):
        """
        Primary method to read and unify engine output.

        Returns:
            self.df (pd.DataFrame): unified dataframe
        """
        logger.remove()
        logger.add(lambda msg: tqdm.write(msg, end=""))
        pbar_iterator = tqdm(
            zip(
                repeat(self.reference_dict),
                self.spectrum_data,
            ),
            total=len(self.spectrum_data),
        )
        with mp.Pool(self.params.get("cpus", mp.cpu_count() - 1)) as pool:
            chunk_dfs = pool.starmap(
                _get_single_spec_df,
                pbar_iterator,
            )
        logger.remove()
        logger.add(sys.stdout)
        self.df = pd.concat(chunk_dfs, axis=0, ignore_index=True)
        self.df.loc[:, "spectrum_title"] = (
            self.df["spectrum_title"]
            .str.replace("%2e", ".", regex=False)
            .str.replace(".mzML", "", regex=False)
        )
        self._format_mods()
        self.process_unify_style()

        return self.df
