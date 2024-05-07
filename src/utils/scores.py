import contextlib
from typing import Callable

import numpy as np
import torch
from rdkit import Chem

from rdkit.Chem.Crippen import MolLogP
from rdkit.Chem.Descriptors import MolWt
from rdkit.Chem import rdFingerprintGenerator
from rdkit.DataStructs import TanimotoSimilarity
from openbabel import pybel
from tdc import Oracle

import math
from pathlib import Path
import subprocess
import random
import shutil

__all__ = [
    "smiles2sa",
    "smiles2qed",
    "smiles2plogp",
    "smiles2gsk3b",
    "smiles2jnk3",
    "smiles2drd2",
    "mol2logp",
    "mol2molwt",
    "smiles2uplogp",
    "smiles2affinity",
    "delta_g_to_kd",
    "molssim",
    "ssim",
    "normalize",
    # global constants
    "PROP_FN",
    "PROTEIN_FILES",
    "MINIMIZE_PROPS",
]

MINIMIZE_PROPS = ["sa", "affinity"]
PROTEIN_FILES = {
    "1err": "data/raw/1err/1err.maps.fld",
    "2iik": "data/raw/2iik/2iik.maps.fld",
}

TIMEOUT = 30

ob_log_handler = pybel.ob.OBMessageHandler()
ob_log_handler.SetOutputLevel(0)

fpg = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)

SmilesScorer = Callable[[str | list[str]], float | list[float]]

smiles2sa: SmilesScorer = Oracle(name="SA")
smiles2qed: SmilesScorer = Oracle(name="QED")
smiles2plogp: SmilesScorer = Oracle(name="LogP")  # This is actually pLogP
smiles2gsk3b: SmilesScorer = Oracle(name="GSK3B")
smiles2jnk3: SmilesScorer = Oracle(name="JNK3")
smiles2drd2: SmilesScorer = Oracle(name="DRD2")


def normalize(x, step_size=None, relative=False):
    if step_size is None:
        return x

    if relative:
        return x * step_size

    try:
        return x / torch.norm(x, dim=-1, keepdim=True) * step_size
    except AttributeError:
        return x


def mol2logp(mol: Chem.Mol) -> float:
    return MolLogP(mol)


def mol2molwt(mol: Chem.Mol) -> float:
    return MolWt(mol)


def smiles2uplogp(smiles: str | list[str]) -> float | list[float]:
    if isinstance(smiles, str):
        return _smiles2uplogp(smiles)
    else:
        return [_smiles2uplogp(s) for s in smiles]


def _smiles2uplogp(smiles: str) -> float:
    """
    Unnormalized pLogP following LIMO.

    Return -100 if the molecule is invalid.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return -100

    logp = mol2logp(mol)
    sa = smiles2sa(smiles)
    plogp = logp - sa

    # method1
    # LIMO use this one
    plogp1 = plogp
    for ring in mol.GetRingInfo().AtomRings():
        if len(ring) > 6:
            plogp1 -= 1
    # method2
    # JT-VAE use this one, common
    plogp2 = plogp
    if cycle_list := mol.GetRingInfo().AtomRings():
        cycle_length = max(len(j) for j in cycle_list)
    else:
        cycle_length = 0
    plogp2 -= max(cycle_length - 6, 0)

    # assert plogp1 == plogp2, f"{plogp1} != {plogp2}"

    return plogp1


def delta_g_to_kd(x: float) -> float:
    return math.exp(x / (0.00198720425864083 * 298.15))


def smiles2affinity(
    smiles: str | list[str],
    protein_file: Path | str = "data/raw/1err/1err.maps.fld",
    autodock: Path | str = "autodock_gpu_128wi",
    output_path: Path | str = "/tmp",
    device_idx: int = 0,
) -> float | list[float]:
    """
    Calculate the binding affinity of a ligand to a protein using AutoDock.

    WARNING: This function is non-deterministic. It may return different results for the
    same input. The reason of non-determinism is that the 3-D structure generated by
    obabel is not deterministic. The docking result indeed deterministic

    As of https://github.com/openbabel/openbabel/issues/1934 is still open, no
    reproducibility is provided by OpenBabel.
    :param smiles:
    :param protein_file:
    :param autodock:
    :param output_path:
    :param device_idx:
    :return:
    """

    _run = random.getrandbits(32)
    output_path = Path(output_path) / f"chemflow_{_run}"
    # shutil.rmtree(output_path, ignore_errors=True)
    output_path.mkdir(parents=True, exist_ok=True)

    try:
        return __smiles2affinity(
            smiles, protein_file, autodock, output_path, device_idx
        )
    finally:
        shutil.rmtree(output_path, ignore_errors=True)


def __smiles2affinity(
    smiles: str | list[str],
    protein_file: Path | str,
    autodock: Path | str,
    output_path: Path | str,
    device_idx: int = 0,
    verbose: bool = False,
):
    single_smiles = isinstance(smiles, str)
    # Only 1 ligand
    if single_smiles:
        smiles = [smiles]

    n = len(smiles)

    if verbose:
        print(f"Running {n} ligands")

    for i, s in enumerate(smiles):
        file_name = output_path / f"{i}.pdbqt"

        # The following code is actually not deterministic
        # mol = pybel.readstring("smi", s)
        # mol.make3D()
        # mol.calccharges(model="gasteiger")
        # mol.write("pdbqt", str(file_name))

        # Gary: this is non-deterministic
        subprocess.run(
            f'obabel -:"{s}" -O {file_name} -p 7.4 --partialcharge gasteiger --gen3d',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    if verbose:
        print("Finished generating all smiles, start docking")

    # Finish generating all smiles, start docking
    with contextlib.suppress(subprocess.TimeoutExpired):
        # Gary: this is also non-deterministic
        if single_smiles:
            subprocess.run(
                f"{autodock} -M {protein_file} -s 0 -D {device_idx + 1} -L {output_path}/0.pdbqt",
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT,
            )
        else:
            subprocess.run(
                f"{autodock} -M {protein_file} -s 0 -D {device_idx + 1} -B {output_path}/*.pdbqt",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT * n,
            )

    if verbose:
        print("Finished docking, start parsing")

    results = np.zeros(n)
    for i in range(n):
        with contextlib.suppress(FileNotFoundError):
            with open(output_path / f"{i}.dlg", "r") as f:
                lines = f.read()
            if "0.000   0.000   0.000  0.00  0.00" in lines:
                continue
            # python implementation of the following command
            # f"grep 'RANKING' outs/{file} | tr -s ' ' | cut -f 5 -d ' ' | head -n 1",
            for line in lines.splitlines():
                if "RANKING" in line:
                    results[i] = float(line.split()[3])
                    break
    return results[0] if single_smiles else results.tolist()


def molssim(mol1: Chem.Mol, mol2: Chem.Mol):
    """
    Structural similarity between two molecules, using Morgan fingerprints
    :return:
    """
    # fps1 = Chem.RDKFingerprint(mol1)
    # fps2 = Chem.RDKFingerprint(mol2)
    # fps1 = GetMorganFingerprintAsBitVect(mol1, 2, 2048)
    # fps2 = GetMorganFingerprintAsBitVect(mol2, 2, 2048)
    fps1 = fpg.GetFingerprint(mol1)
    fps2 = fpg.GetFingerprint(mol2)
    # fps1 = FingerprintMol(mol1)
    # fps2 = FingerprintMol(mol2)
    return TanimotoSimilarity(fps1, fps2)


def ssim(smiles1: str, smiles2: str):
    """
    Structural similarity between two SMILES strings, using Morgan fingerprints
    :return:
    """
    mol1 = Chem.MolFromSmiles(smiles1)
    mol2 = Chem.MolFromSmiles(smiles2)
    return molssim(mol1, mol2)


PROP_FN = {
    "sa": smiles2sa,
    "qed": smiles2qed,
    "plogp": smiles2plogp,
    "uplogp": smiles2uplogp,
    "gsk3b": smiles2gsk3b,
    "jnk3": smiles2jnk3,
    "drd2": smiles2drd2,
    "affinity": smiles2affinity,
}

if __name__ == "__main__":
    SMILES = [
        "CCCCCC",
        "C1CCCCC1",
        "CCN(CCCCl)OC1=CC2=C(Cl)C1C3=C2CCCO3",
        "Cc1ccccc1CC(=O)NC[C@@H]1CN(Cc2ccccc2)CCO1",
        "Cc1cccc(N2CCN(C(=O)C[NH+](C)CCO)CC2)c1",
        "CCC[C@H](NC(=O)CSc1ncn[nH]1)c1ccccc1",
        "Cc1cc(N2CCC[C@H]2c2cc(CCC(N)=O)nc(C)n2)ncn1",
        "CO[C@H]1C[NH2+]C[C@@H]1Nc1cc(C)c([N+](=O)[O-])cn1",
        "COC(=O)C[C@H](C)[S@](=O)[C@H](C)c1nc(-c2ccccc2C)no1",
        "CCOCc1ccccc1CNC(=O)C[NH+]1CC[C@@H](C)[C@H](O)C1",
        "CCc1ccc(C2=CCN(C(=O)NC[C@H](C)CC3(C)OCCO3)CC2)cc1",
        "O=C(NCc1nc(-c2ccco2)n[nH]1)N1CCOC[C@H]1C1CC1",
        "CN(C)c1nnc(S[C@H]2CCC[C@@]([NH3+])(CO)C2)s1",
        "CC(=O)c1cccc(NC(=O)C2CCN(S(=O)(=O)c3cc(-c4noc(C)n4)ccc3C)CC2)c1",
        "COC(=O)[C@H]1C(=O)Oc2ccc(Br)cc2[C@@H]1[C@H](C)C(=O)c1ccc(C)cc1",
        "CCCCSc1nc2n(n1)[C@@H](c1sccc1C)C(C(=O)OC)=C(C)N2",
        "CC(=O)Nc1cc(C(=O)NCc2ccncc2)nn1-c1ccccc1",
        "COC(=O)[C@@H](C)Oc1ccc(C[NH+]2CCC(c3n[nH]c(C)n3)CC2)cc1",
        "COc1ccc(CCC(=O)Nc2ccc(C)c(N3CCCCC3=O)c2)cc1",
        "Cn1c(SCC(=O)N2CCC[C@@H](C(F)(F)F)C2)nnc1C1CC1",
        "CCc1nn(-c2cccc(C(F)(F)F)c2)c(CC)c1C[NH3+]",
        "N#Cc1cccc(NC(=O)c2cc(Cl)nc(Cl)n2)c1",
        "O=C(CNC(=O)N1CCC(c2nc3ccccc3o2)CC1)N1CCCCC1",
        "CC(C)[C@@H]1C[NH+]2CCCC[C@@H]2CN1C[C@@H]1CCS(=O)(=O)C1",
        "Cc1ccc(Cn2nc(-c3ccc(Br)cc3)ccc2=O)cc1",
        "O=[N+]([O-])c1cc2c(cc1-c1ccc(/C=C3\SC(=S)N=C3[O-])o1)OCO2",
        "O=C(NCc1ccco1)C1CCN(C(=O)N2CCOc3ccc(Cl)cc32)CC1",
        "Cc1nc2sccn2c1C[NH+]1CCC([C@H](Cc2ccccc2)N(C)C(=O)C2CCOCC2)CC1",
        "CC(C)C[C@H](O)c1ccc(C(F)(F)F)cc1",
        "C[C@@H](O)CC[NH2+][C@@H](c1ccc(Cl)c(Cl)c1)C1CC1",
        "O=C(NCC[C@H]1CCCCN1S(=O)(=O)c1cccs1)c1cc(Cl)ccc1Cl",
        "C[C@@]12CCC(=O)C=C1CC[C@@H]1[C@@H]2CC[C@]2(C)[C@H]1CC[C@]2(C)O",
        "CC(C)N(CCC#N)Cc1cc(N)cc[nH+]1",
        "Cc1ccc(CC(=O)NNC(=O)CCSc2ccc(F)cc2)cc1O",
        "Cn1cc(C#N)cc1C(=O)N[C@H](C#N)c1ccc(F)cc1F",
        "O=C1CCCN1C1CC[NH+](Cc2ccc(-c3c(F)cccc3F)o2)CC1",
        "COc1cccc(NC(=O)[C@H]2CCCN(c3nc4ccc(C)cc4s3)C2)c1",
    ]
    # print(len(SMILES))
    # print(SMILES[29])
    # # SMILES = "CC(C)N(CCC#N)Cc1cc(N)cc[nH+]1"
    # # SMILES = "CCN(CCCCl)OC1=CC2=C(Cl)C1C3=C2CCCO3"
    # # SMILES = "C[C@@]12CCC(=O)C=C1CC[C@@H]1[C@@H]2CC[C@]2(C)[C@H]1CC[C@]2(C)O"
    # print(smiles2affinity(SMILES))

    mol1 = Chem.MolFromSmiles(SMILES[0])
    mol2 = Chem.MolFromSmiles(SMILES[1])
    mol3 = Chem.MolFromSmiles(SMILES[2])
    mol4 = Chem.MolFromSmiles(SMILES[3])

    # print(ssim(mol1, mol2))
    # print(mol2sa(mol1))
    # print(smiles2sa(SMILES[0]))

    # print(mol2logp(mol1))
    # print(smiles2plogp(SMILES[0]))
    # print(smiles2drd2(SMILES[0]))

    print(ssim(SMILES[0], SMILES[1]))

    # print(smiles2plogp('N#Cc1ccc2c(c1)OCCOCCOCCOc1ccc(C#N)cc1OCCOCCOCCO2'))
    # print(smiles2plogp('COC(=O)c1cc2c(C(=O)OC)cc1CSCc1cccc(n1)CSC2'))

    # smiles = "C=CCC(CCCC)C=CCC(C)C=CC(=C)N=CC=CC=CC1=CC=CC=C1"
    smiles = "CCCCC[C@H1](C[C@@H1][C@H1]CCCCCCCCCCCCC)CCCCCC=CC1=CCCC=C1"  # limo SGD
    # smiles = "CCCCC=C(C)CCC(=O)C(=C)CC(C)(CC1CCCCC1)CCCSSCCCCC=C"
    print(smiles2uplogp(smiles))
    print(smiles2plogp(smiles))

    print([smiles2uplogp(s) for s in SMILES])
