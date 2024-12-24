
import pandas as pd
import numpy as np
from datetime import timedelta
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RawDataAssembler:
    """A class to manage the raw data assembly and storage process"""

    def __init__(self, wildfire_incidence_data, start_date, end_date, resample_interval):
        self.wildfire_incidence_data = wildfire_incidence_data
        self.start_date = start_date
        self.end_date = end_date
        self.resample_interval = resample_interval
        self.dataset = None


    ## assemble_dataset method
    ##      - assemble the dataset using the specified data pipelines
    ##      - pipelines is a list of dictionaries with the following keys:
    ##          - 'CDS': Copernicus Data Service pipeline
    ##          - Future data pipelines will be added as needed
    ##      - grouping_period_size is the temporal grouping period for the dataset (i.e., temporal size of the output CSVs) 
    ##        must be one of the following:
    ##          - 'D': daily grouping
    ##          - 'W': weekly grouping
    ##          - 'M': monthly grouping
    ##          - 'Y': yearly grouping
    ##      - output: None
    ##      - mutates self.dataset (combining dataframes as they are assembled)
    def assemble_dataset(self, pipelines, grouping_period_size, latitude_tolerance=1.0, longitude_tolerance=1.0):
        """Assemble the dataset using the specified data pipelines"""

        logger.info(f"Wildfire Incidence Data Columns in Assembler: {self.wildfire_incidence_data.columns}")

        # Separate fire dates with location data (exclude non-fire dates)
        fire_dates = self.wildfire_incidence_data.dropna(subset=['fire_location_latitude', 'fire_location_longitude'])
        logger.info(f"Number of fire dates with location data: {len(fire_dates)}")

        # Optionally, print first few fire_dates
        logger.debug(f"Sample fire_dates:\n{fire_dates.head()}")

        # Generate a DataFrame with all dates (fire and non-fire) for the specified period
        self.all_dates_df = self._all_dates_generator(
            start_date=self.start_date,
            end_date=self.end_date,
            interval=self.resample_interval,
            fire_dataset=self.wildfire_incidence_data
        )

        # Group all dates by the specified period size
        try:
            grouped_all_dates = self.all_dates_df.groupby(
                self.all_dates_df['date'].dt.to_period(grouping_period_size)
            )
            logger.info(f"Grouped all_dates data by period: {grouping_period_size}")
        except KeyError as e:
            logger.error(f"Grouping failed due to missing key: {e}")
            return

        for pipeline in pipelines:
            if 'CDS' in pipeline:
                cds_pipeline = pipeline['CDS']
                logger.info("CDS pipeline found!")

                for period, batch in grouped_all_dates:
                    start_date = batch['date'].min()
                    end_date = batch['date'].max()
                    logger.info(f"Starting request for weather data from {start_date} to {end_date}")

                    weather_data = cds_pipeline.fetch_weather_data(start_date, end_date)  # fetch_weather_data returns a DataFrame or None

                    if weather_data is None:
                        logger.error(f"Failed to fetch weather data for period {period}. Skipping this batch.")
                        continue  # Skip to the next batch

                    # Check if 'date' column exists
                    if 'date' not in weather_data.columns:
                        logger.error("Weather data does not contain 'date' column. Skipping this batch.")
                        continue

                    try:
                        weather_data['date'] = weather_data['date'].dt.date
                    except Exception as e:
                        logger.error(f"Error converting 'date' column: {e}")
                        continue

                    # Optionally, print sample weather_data
                    logger.debug(f"Sample weather_data:\n{weather_data[['date', 'latitude', 'longitude']].head()}")

                    logger.info(f"Processing weather data from {start_date} to {end_date}, Data shape: {weather_data.shape}")

                    # Label fire days for the current batch by matching both date and location with a proximity check
                    weather_data['is_fire_day'] = weather_data.apply(
                        lambda row: self._is_fire_labeler(
                            row, 
                            fire_dates,  # Pass fire_dates DataFrame from wildfire_incidence_data
                            latitude_tolerance, 
                            longitude_tolerance
                        ), 
                        axis=1
                    ) ## NOTE: UNSURE IF THIS WILL WORK
                    
                    # Check how many fire days were found
                    num_fire_days = weather_data['is_fire_day'].sum()
                    logger.info(f"Number of fire days found in this batch: {num_fire_days}")

                    # Generate the target file name for the weather data
                    target_file = f"weather_data_{period.strftime('%Y%m')}.csv"

                    # Save the DataFrame to a CSV file labeled as above
                    try:
                        weather_data.to_csv(target_file, index=False)
                        logger.info(f"Weather data saved to '{target_file}'.")
                    except Exception as e:
                        logger.error(f"Failed to save weather data to '{target_file}': {e}")


    ## _is_fire_labeler method
    ##      - label the fire incidents in the dataset within a specified location tolerance
    ##      - input: row, fire_dates, latitude_tolerance, longitude_tolerance
    ##      - output: 1 if a matching fire is found, 0 otherwise
    ## Note this method checks for a matching fire incident in the fire_dates DataFrame (goes through all of it each time its called)
    def _is_fire_labeler(self, row, fire_dates, latitude_tolerance, longitude_tolerance):
        """Label the fire incidents in the dataset within a specified location tolerance.
        Args:
            row: A single row of the DataFrame (supplied automatically by `apply`).
            fire_dates: DataFrame containing fire incident data with columns:
                - fire_start_date
                - fire_location_latitude
                - fire_location_longitude
            latitude_tolerance: Latitude tolerance for matching locations.
            longitude_tolerance: Longitude tolerance for matching locations.

        Returns:
            int: 1 if a matching fire is found, 0 otherwise.
        """
        matching_fires = fire_dates[
            (fire_dates['fire_start_date'] == row['date']) &
            (fire_dates['fire_location_latitude'].between(row['latitude'] - latitude_tolerance,
                                                          row['latitude'] + latitude_tolerance)) &
            (fire_dates['fire_location_longitude'].between(row['longitude'] - longitude_tolerance,
                                                           row['longitude'] + longitude_tolerance))
        ]

        # Debug logging
        if matching_fires.empty:
            logger.debug(f"No fire match found for date {row['date']} and location ({row['latitude']}, {row['longitude']})")
        else:
            logger.debug(f"Fire match found for date {row['date']} and location ({row['latitude']}, {row['longitude']})")

        return int(not matching_fires.empty)
    

    ## all_dates_generator
    ##      - generates a dataframe of dates with all fire dates in fire_dataset and non-fire dates resampled to the specified interval
    ##      - input: start_date, end_date, interval (e.g., '4D' is every 4th day), fire_dataset
    ##      - output: all_dates_df (pandas DataFrame)
    ##      - private method
    def _all_dates_generator(self, start_date, end_date, interval, fire_dataset):

        # Filter the fire dates data to only include the relevant columns and remove rows with missing values
        fire_dates = fire_dataset[['fire_start_date', 'fire_location_latitude', 'fire_location_longitude']].dropna()

        # Create a DataFrame that contains every nth (interval) day from start_date to end_date
        all_dates = pd.date_range(start=start_date, end=end_date, freq=interval).normalize()
        all_dates = pd.Series(list(set(all_dates).union(fire_dates['fire_start_date']))).sort_values()

        # Ensure all dates are within the range start_date to end_date
        all_dates = all_dates[(all_dates >= pd.Timestamp(start_date)) & (all_dates <= pd.Timestamp(end_date))]

        # Create DataFrame for all dates without fire day labels (labeling will be done later)
        all_dates_df = pd.DataFrame({'date': all_dates})
        logger.info(f"all_dates count (constructed from fire_dates + every nth (interval) day): {len(all_dates_df)}")
        logger.debug(f"Sample all_dates:\n{all_dates_df.head()}")

        return all_dates_df