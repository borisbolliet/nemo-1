#!/bin/sh
#SBATCH --nodes=22
#SBATCH --ntasks-per-node=16
#SBATCH --mem=64000
#SBATCH --time=10:00:00

#source ~/.bashrc
source /home/mjh/SETUP_CONDA.sh
time mpiexec nemo MFMF_SOSim_3freq_tiles.yml -M -n
