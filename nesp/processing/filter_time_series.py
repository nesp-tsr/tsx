from nesp.db import get_session
import logging
import nesp.config

log = logging.getLogger(__name__)

def process_database():
	session = get_session()

	log.info("Step 1/1 - Building list of filtered time series")

	# Get year range
	min_year = nesp.config.config.getint("processing", "min_year")
	max_year = nesp.config.config.getint("processing", "max_year")

	session.execute("""CREATE TEMPORARY TABLE tmp_filtered_ts
		( INDEX (time_series_id) )
		SELECT time_series_id
		FROM aggregated_by_year agg
		INNER JOIN taxon ON agg.taxon_id = taxon.id
		LEFT JOIN data_source ON data_source.taxon_id = agg.taxon_id AND data_source.source_id = agg.source_id
		WHERE agg.start_date_y <= :max_year
		AND agg.start_date_y >= :min_year
		AND COALESCE(agg.search_type_id, 0) != 6
		AND GREATEST(COALESCE(taxon.epbc_status_id, 0), COALESCE(taxon.iucn_status_id, 0), COALESCE(taxon.aust_status_id, 0)) NOT IN (0,1,7)
		AND region_id IS NOT NULL
		AND COALESCE(data_source.data_agreement_id, -1) NOT IN (0, 1)
		AND COALESCE(data_source.standardisation_of_method_effort_id, -1) NOT IN (0, 1)
		AND COALESCE(data_source.consistency_of_monitoring_id, -1) NOT IN (0, 1)
		GROUP BY agg.time_series_id
		HAVING MAX(value) > 0
		AND COUNT(DISTINCT start_date_y) >= 5;
	""", {
		'min_year': min_year,
		'max_year': max_year
	})

	log.info("Step 2/2 - Updating aggregated_by_year table")

	session.execute("""UPDATE aggregated_by_year SET include_in_analysis = time_series_id IN (SELECT time_series_id FROM tmp_filtered_ts)""")
	session.execute("""DROP TABLE tmp_filtered_ts""")

	log.info("Done")