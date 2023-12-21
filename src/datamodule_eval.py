"""
LightningDataModule to load Earth Observation data from GeoTIFF files using
rasterio.
"""
import math
import os
import random
from pathlib import Path
from typing import List, Literal

import glob
import lightning as L
import numpy as np
import rasterio
import rioxarray
import torch
import torchdata
from torch.utils.data import DataLoader, Dataset
from torchvision.transforms import v2

os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
os.environ["GDAL_HTTP_MERGE_CONSECUTIVE_RANGES"] = "YES"

class ClayDataset(Dataset):
    def __init__(self, chips_path, chips_label_path, transform=None):
        super().__init__()
        self.chips_path = chips_path,
        self.chips_label_path = chips_label_path
        self.transform = transform

    def normalize_timestamp(self, ts):
        year, month, day = map(np.float16, ts.split("-"))
        year_radians = 2 * math.pi * (year - 2012) / (2030 - 2012)  # years 2012-2030
        month_radians = 2 * math.pi * (month - 1) / 11
        day_radians = (
            2 * math.pi * (day - 1) / 30
        )  # Assuming a 31-day month for simplicity

        # Normalize using sine and cosine
        year = math.atan2(math.cos(year_radians), math.sin(year_radians))
        month = math.atan2(math.cos(month_radians), math.sin(month_radians))
        day = math.atan2(math.cos(day_radians), math.sin(day_radians))

        return year, month, day

    def normalize_latlon(self, lon, lat):
        lon_radians = math.radians(lon)
        lat_radians = math.radians(lat)

        # Apply sine and cosine
        lon = math.atan2(
            math.cos(lon_radians),
            math.sin(lon_radians),
        )
        lat = math.sin(lat_radians)
        return lon, lat

    def read_chip(self, chip_path, chip_path_label, date, bounds, centroid, epsg):
        chip = chip_path # rasterio.open(chip_path)
        chip_label = chip_path_label # rasterio.open(chip_path_label)

        # read timestep & normalize
        year, month, day = self.normalize_timestamp(date)

        # read lat,lon from UTM to WGS84 & normalize
        lon, lat = centroid[0], centroid[1],  # longitude, latitude
        lon, lat = self.normalize_latlon(lon, lat)

        return {
            "labels": chip_label,
            "pixels": chip, #chip.read(),
            # Raw values
            "bbox": bounds,
            "epsg": epsg,
            "date": date,
            # Normalized values
            "latlon": (lat, lon),
            "timestep": (year, month, day),
        }
    
    
    def get_image_granules(self, chips_path, chips_label_path, idx):

        chip_path = chips_path[idx]
        chip_label_path = chips_label_path[idx]
        chip_path = chip_path[0]

        position = "_".join(chip_path.split("/")[-1].split("_")[-3:-1])
        date = "_".join(chip_path.split("/")[-1].split("_"))
        date = chip_path.split("/")[-1].split("_")[-4][:8]
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        flood_event = chip_path.split("/")[-2]

        chip_data_array = rasterio.open(chip_path)
        filename = "_".join(chip_path.split("/")[-1].split("_"))[:-4]
        image_array_values = chip_data_array.read()
        # Get bounds
        bounds = chip_data_array.bounds
        # Get centroid
        # Calculate centroid
        xmin, ymin, xmax, ymax = bounds
        centroid_x = (xmin + xmax) / 2.0
        centroid_y = (ymin + ymax) / 2.0
        centroid = (centroid_x, centroid_y)
        # Get EPSG
        epsg = chip_data_array.crs.to_epsg()
        chip_label_path_data_array = rasterio.open(chip_label_path)
        label_array_values = chip_label_path_data_array.read()
        return image_array_values, label_array_values, flood_event, position, date, bounds, centroid, epsg, filename
    
    def get_benchmark_data(self, chips_path, chips_label_path, idx):
        image_array_values, label_array_values, flood_events, positions, dates, bounds_, centroids, epsgs, filenames  = \
        self.get_image_granules(chips_path, chips_label_path, idx)
        return image_array_values, label_array_values, flood_events, positions, dates, bounds_, centroids, epsgs, filenames

    def __getitem__(self, idx):
        #image_array_values, label_array_values, flood_events, positions, dates, bounds_, centroids, epsgs, filenames = \
        #    self.get_benchmark_data(self.chips_path, self.chips_label_path)
        image_array_values, label_array_values, flood_event, position, date, bounds, centroid, epsg, filename = \
            self.get_benchmark_data(self.chips_path, self.chips_label_path, idx)
        cube = self.read_chip(image_array_values, label_array_values, date, bounds, centroid, epsg)

        # remove nans and convert to tensor
        cube["labels"] = torch.as_tensor(data=cube["labels"], dtype=torch.float32)
        cube["pixels"] = torch.as_tensor(data=cube["pixels"], dtype=torch.float32)
        cube["bbox"] = torch.as_tensor(data=cube["bbox"], dtype=torch.float64)
        cube["epsg"] = torch.as_tensor(data=cube["epsg"], dtype=torch.int32)
        cube["date"] = str(cube["date"])
        cube["latlon"] = torch.as_tensor(data=cube["latlon"])
        cube["timestep"] = torch.as_tensor(data=cube["timestep"])
        try:
            cube["source_url"] = str(self.chip_path.absolute())
        except AttributeError:
            cube["source_url"] = filename #chip_path

        if self.transform:
            # convert to float16 and normalize
            cube["pixels"] = self.transform(cube["pixels"])

        return cube

    def __len__(self):
        return len(self.chips_path)


class ClayDataModule(L.LightningDataModule):
    MEAN = [
        518.393981,
        670.384338,
        583.347534,
        961.506958,
        1903.755737,
        2138.707519,
        2238.332031,
        2273.117919,
        1413.791137,
        808.279968,
        0.033653,
        0.135196,
        536.390136,
    ]

    STD = [
        876.523559,
        918.090148,
        981.493835,
        1001.560729,
        1256.656372,
        1346.299072,
        1414.495483,
        1392.251342,
        918.297912,
        605.479919,
        0.048188,
        0.380075,
        630.602233,
    ]

    def __init__(
        self,
        data_dir: str = "data",
        batch_size: int = 4,
        num_workers: int = 8,
    ):
        super().__init__()
        self.data_dir = data_dir
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.split_ratio = 0.8
        self.tfm = v2.Compose([v2.Normalize(mean=self.MEAN, std=self.STD)])

    def setup(self, stage='fit'):
        # Get list of GeoTIFF filepaths from s3 bucket or data/ folder
        #if self.data_dir.startswith("s3://"):
        #    dp = torchdata.datapipes.iter.IterableWrapper(iterable=[self.data_dir])
        #    chips_path = list(dp.list_files_by_s3(masks="*.tif"))
        #else:  # if self.data_dir is a local data path

        chips_path = glob.glob(f"{self.data_dir}/**/*_rtc.tif") #list(Path(self.data_dir).glob("**/*_rtc.tif"))
        chips_label_path = glob.glob(f"{self.data_dir}/**/*_LabelWater.tif")#list(Path(self.data_dir).glob("**/*_LabelWater.tif"))
        print(f"Total number of chips: {len(chips_path)}")
        #print(f"All chips: {chips_path}")

        if stage == "fit":
            #random.shuffle(chips_path)
            split = int(len(chips_path) * self.split_ratio)
            #print(chips_path[:split], chips_label_path[:split])

            self.trn_ds = ClayDataset(chips_path=chips_path[:split], chips_label_path=chips_label_path[:split], transform=self.tfm)
            self.val_ds = ClayDataset(chips_path=chips_path[split:], chips_label_path=chips_label_path[:split], transform=self.tfm)

        elif stage == "predict":
            self.prd_ds = ClayDataset(chips_path=chips_path, transform=self.tfm)

    def train_dataloader(self):
        return DataLoader(
            self.trn_ds,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=True,
            pin_memory=True,
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_ds,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=False,
            pin_memory=True,
        )

    def predict_dataloader(self):
        return DataLoader(
            dataset=self.prd_ds,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=False,
        )


# %%
# Torchdata-based approach
def _array_to_torch(filepath):
    """
    Read a GeoTIFF file using rasterio into a numpy.ndarray, convert it to a
    torch.Tensor (float16 dtype), and also output spatiotemporal metadata
    associated with the image.

    Parameters
    ----------
    filepath : str
        The path to the GeoTIFF file.

    Returns
    -------
    outputs : dict
        A dictionary containing the following items:
        - image: torch.Tensor - multi-band raster image with shape (Band, Height, Width)
        - bbox: torch.Tensor - spatial bounding box as (xmin, ymin, xmax, ymax)
        - epsg: torch.Tensor - coordinate reference system as an EPSG code
        - date: str - the date the image was acquired in YYYY-MM-DD format
        - source_url: str - the URL or path to the source GeoTIFF file
    """
    # GeoTIFF - Rasterio
    with rasterio.open(fp=filepath) as dataset:
        # Get image data
        array: np.ndarray = dataset.read()
        tensor: torch.Tensor = torch.as_tensor(data=array.astype(dtype="float16"))

        # Get spatial bounding box and coordinate reference system in UTM projection
        bbox: torch.Tensor = torch.as_tensor(  # xmin, ymin, xmax, ymax
            data=dataset.bounds, dtype=torch.float64
        )
        epsg: int = torch.as_tensor(data=dataset.crs.to_epsg(), dtype=torch.int32)

        # Get date
        date: str = dataset.tags()["date"]  # YYYY-MM-DD format

    return {
        "image": tensor,  # shape (13, 512, 512)
        "bbox": bbox,  # bounds [xmin, ymin, xmax, ymax]
        "epsg": epsg,  # e.g. 32632
        "date": date,  # e.g. 2020-12-31
        "source_url": filepath,  # e.g. s3://.../claytile_12ABC_20201231_v0_0200.tif
    }


class GeoTIFFDataPipeModule(L.LightningDataModule):
    """
    LightningDataModule for loading GeoTIFF files.

    Uses torchdata.
    """

    def __init__(
        self,
        data_dir: str = "data/",
        batch_size: int = 32,
        num_workers: int = 8,
    ):
        """
        Go from datacubes to 512x512 chips!

        Parameters
        ----------
        data_dir : str
            Path to the data folder where the GeoTIFF files are stored. Default
            is 'data/'.
        batch_size : int
            Size of each mini-batch. Default is 32.
        num_workers : int
            How many subprocesses to use for data loading. 0 means that the
            data will be loaded in the main process. Default is 8.

        Returns
        -------
        datapipe : torchdata.datapipes.iter.IterDataPipe
            A torch DataPipe that can be passed into a torch DataLoader.
        """
        super().__init__()
        self.data_dir: str = data_dir
        self.batch_size: int = batch_size
        self.num_workers: int = num_workers

    def setup(self, stage):
        """
        Data operations to perform on every GPU.
        Split data into training and test sets, etc.

        Parameters
        ----------
        stage : str or None
            Whether to setup the datapipe for the training/validation loop, or
            the prediction loop. Choose from either 'fit' or 'predict'.
        """
        # Step 1 - Get list of GeoTIFF filepaths from s3 bucket or data/ folder
        if self.data_dir.startswith("s3://"):
            dp = torchdata.datapipes.iter.IterableWrapper(iterable=[self.data_dir])
            self.dp_paths = dp.list_files_by_s3(masks="*.tif")
        else:  # if self.data_dir is a local data path
            self.dp_paths = torchdata.datapipes.iter.FileLister(
                root=self.data_dir, masks="*.tif", recursive=True
            )

        if stage == "fit":  # training/validation loop
            # Step 2 - Split GeoTIFF chips into train/val sets (80%/20%)
            # https://pytorch.org/data/0.7/generated/torchdata.datapipes.iter.RandomSplitter.html
            dp_train, dp_val = self.dp_paths.random_split(
                weights={"train": 0.8, "validation": 0.2}, total_length=423, seed=42
            )

            # Step 3 - Read GeoTIFF into numpy array, batch and convert to torch.Tensor
            self.datapipe_train = (
                dp_train.sharding_filter()
                .map(fn=_array_to_torch)
                .batch(batch_size=self.batch_size)
                .collate()
            )
            self.datapipe_val = (
                dp_val.sharding_filter()
                .map(fn=_array_to_torch)
                .batch(batch_size=self.batch_size)
                .collate()
            )

        elif stage == "predict":  # prediction loop
            self.datapipe_predict = (
                self.dp_paths.sharding_filter()
                .map(fn=_array_to_torch)
                .batch(batch_size=self.batch_size)
                .collate()
            )

    def train_dataloader(self) -> torch.utils.data.DataLoader:
        """
        Loads the data used in the training loop.
        """
        return torch.utils.data.DataLoader(
            dataset=self.datapipe_train,
            batch_size=None,  # handled in datapipe already
            num_workers=self.num_workers,
        )

    def val_dataloader(self) -> torch.utils.data.DataLoader:
        """
        Loads the data used in the validation loop.
        """
        return torch.utils.data.DataLoader(
            dataset=self.datapipe_val,
            batch_size=None,  # handled in datapipe already
            num_workers=self.num_workers,
        )

    def predict_dataloader(self) -> torch.utils.data.DataLoader:
        """
        Loads the data used in the prediction loop.
        """
        return torch.utils.data.DataLoader(
            dataset=self.datapipe_predict,
            batch_size=None,  # handled in datapipe already
            num_workers=self.num_workers,
        )