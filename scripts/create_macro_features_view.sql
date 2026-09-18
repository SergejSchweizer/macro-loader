-- Run as the PostgreSQL administrator against the macro_loader database.
CREATE OR REPLACE FUNCTION macro_loader.rebuild_macro_features()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    base_column text;
    series_id text;
    first_timestamp timestamptz;
    ctes text := '';
    selected_columns text := '';
    joins text := '';
BEGIN
    FOR base_column IN
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'macro_loader'
          AND table_name = 'macro_raw'
          AND column_name LIKE '%\_level' ESCAPE '\'
        ORDER BY ordinal_position
    LOOP
        EXECUTE format(
            'SELECT min(timestamp_m1) FROM macro_loader.macro_raw WHERE %I IS NOT NULL',
            base_column
        ) INTO first_timestamp;

        IF first_timestamp IS NOT NULL THEN
            series_id := regexp_replace(base_column, '_level$', '');
            ctes := ctes || CASE WHEN ctes = '' THEN '' ELSE ', ' END || format(
                $sql$
                %1$I_source AS (
                    SELECT timestamp_m1, %2$I AS level,
                        lag(%2$I, 1) OVER ordered AS lag_1,
                        lag(%2$I, 5) OVER ordered AS lag_5,
                        lag(%2$I, 20) OVER ordered AS lag_20
                    FROM macro_loader.macro_raw
                    WHERE %2$I IS NOT NULL
                    WINDOW ordered AS (ORDER BY timestamp_m1)
                ),
                %1$I_changes AS (
                    SELECT *,
                        level - lag_1 AS change,
                        lag(level - lag_1, 1) OVER (ORDER BY timestamp_m1) AS change_lag_1,
                        lag(level - lag_1, 5) OVER (ORDER BY timestamp_m1) AS change_lag_5,
                        lag(level - lag_1, 20) OVER (ORDER BY timestamp_m1) AS change_lag_20,
                        CASE WHEN level > 0 AND lag_1 > 0 THEN ln(level / lag_1) END AS log_return
                    FROM %1$I_source
                ),
                %1$I_features AS (
                    SELECT timestamp_m1,
                        level - lag_1 AS %1$I_delta_1obs,
                        level - lag_5 AS %1$I_delta_5obs,
                        level - lag_20 AS %1$I_delta_20obs,
                        CASE WHEN count(level) OVER window_60 = 60
                                  AND stddev_pop(level) OVER window_60 <> 0
                             THEN (level - avg(level) OVER window_60) / stddev_pop(level) OVER window_60
                        END AS %1$I_zscore_60obs,
                        CASE WHEN count(change) OVER window_60 = 60
                                  AND count(change_lag_1) OVER window_60 = 60
                             THEN greatest(corr(change, change_lag_1) OVER window_60, 0.0)
                        END AS %1$I_momentum_autocorr_1_60obs,
                        CASE WHEN count(change) OVER window_60 = 60
                                  AND count(change_lag_5) OVER window_60 = 60
                             THEN greatest(corr(change, change_lag_5) OVER window_60, 0.0)
                        END AS %1$I_momentum_autocorr_5_60obs,
                        CASE WHEN count(change) OVER window_120 = 120
                                  AND count(change_lag_20) OVER window_120 = 120
                             THEN greatest(corr(change, change_lag_20) OVER window_120, 0.0)
                        END AS %1$I_momentum_autocorr_20_120obs,
                        CASE WHEN count(log_return) OVER window_10 = 10
                             THEN (exp(avg(log_return) OVER window_10) - 1.0) * 100.0 END
                             AS %1$I_return_geom_10obs_pct,
                        CASE WHEN count(log_return) OVER window_25 = 25
                             THEN (exp(avg(log_return) OVER window_25) - 1.0) * 100.0 END
                             AS %1$I_return_geom_25obs_pct,
                        CASE WHEN count(log_return) OVER window_60 = 60
                             THEN (exp(avg(log_return) OVER window_60) - 1.0) * 100.0 END
                             AS %1$I_return_geom_60obs_pct,
                        CASE WHEN count(log_return) OVER window_120 = 120
                             THEN (exp(avg(log_return) OVER window_120) - 1.0) * 100.0 END
                             AS %1$I_return_geom_120obs_pct,
                        CASE WHEN count(log_return) OVER window_240 = 240
                             THEN (exp(avg(log_return) OVER window_240) - 1.0) * 100.0 END
                             AS %1$I_return_geom_240obs_pct
                    FROM %1$I_changes
                    WINDOW
                        window_10 AS (ORDER BY timestamp_m1 ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
                        window_25 AS (ORDER BY timestamp_m1 ROWS BETWEEN 24 PRECEDING AND CURRENT ROW),
                        window_60 AS (ORDER BY timestamp_m1 ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
                        window_120 AS (ORDER BY timestamp_m1 ROWS BETWEEN 119 PRECEDING AND CURRENT ROW),
                        window_240 AS (ORDER BY timestamp_m1 ROWS BETWEEN 239 PRECEDING AND CURRENT ROW)
                )
                $sql$,
                series_id,
                base_column
            );
            selected_columns := selected_columns || format(
                ', %1$I.%2$I_delta_1obs, %1$I.%2$I_delta_5obs, %1$I.%2$I_delta_20obs,
                 %1$I.%2$I_zscore_60obs, %1$I.%2$I_momentum_autocorr_1_60obs,
                 %1$I.%2$I_momentum_autocorr_5_60obs, %1$I.%2$I_momentum_autocorr_20_120obs,
                 %1$I.%2$I_return_geom_10obs_pct, %1$I.%2$I_return_geom_25obs_pct,
                 %1$I.%2$I_return_geom_60obs_pct, %1$I.%2$I_return_geom_120obs_pct,
                 %1$I.%2$I_return_geom_240obs_pct',
                series_id || '_features',
                series_id
            );
            joins := joins || format(
                ' LEFT JOIN %1$I ON %1$I.timestamp_m1 = raw.timestamp_m1',
                series_id || '_features'
            );
        END IF;
    END LOOP;

    IF ctes = '' THEN
        RAISE EXCEPTION 'macro_raw has no nonempty level columns';
    END IF;

    DROP MATERIALIZED VIEW IF EXISTS macro_loader.macro_features;
    EXECUTE format(
        'CREATE MATERIALIZED VIEW macro_loader.macro_features AS WITH %s
         SELECT raw.timestamp_m1%s,
             CASE WHEN raw.vix_level > 0 THEN raw.vix9d_level / raw.vix_level END
                 AS vix9d_vix_ratio,
             CASE WHEN raw.vix3m_level > 0 THEN raw.vix_level / raw.vix3m_level END
                 AS vix_vix3m_ratio,
             raw.vix3m_level - raw.vix_level AS vix3m_minus_vix,
             raw.vix6m_level - raw.vix_level AS vix6m_minus_vix,
             raw.vix1y_level - raw.vix_level AS vix1y_minus_vix,
             raw.us_10y_level - raw.us_2y_level AS us_10y_minus_us_2y
         FROM macro_loader.macro_raw AS raw%s
         WHERE raw.timestamp_m1 >= ''2010-01-01 00:00:00+00''::timestamptz',
        ctes,
        selected_columns,
        joins
    );
END;
$$;

SELECT macro_loader.rebuild_macro_features();
ALTER MATERIALIZED VIEW macro_loader.macro_features OWNER TO "macro-loader-owner";
GRANT SELECT ON macro_loader.macro_features TO "macro-loader", "macro-loader-sync";

CREATE OR REPLACE FUNCTION macro_loader.refresh_macro_features()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, macro_loader
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW macro_loader.macro_features;
    RETURN NULL;
END;
$$;

ALTER FUNCTION macro_loader.refresh_macro_features() OWNER TO "macro-loader-owner";
REVOKE ALL ON FUNCTION macro_loader.refresh_macro_features() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION macro_loader.refresh_macro_features()
    TO "macro-loader", "macro-loader-sync";

DROP TRIGGER IF EXISTS refresh_macro_features_after_raw_change ON macro_loader.macro_raw;
CREATE TRIGGER refresh_macro_features_after_raw_change
AFTER INSERT OR UPDATE OR DELETE ON macro_loader.macro_raw
FOR EACH STATEMENT
EXECUTE FUNCTION macro_loader.refresh_macro_features();