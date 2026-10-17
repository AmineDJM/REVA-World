from test_atlas_imf import weo, raws  # noqa
import polars as pl, pytest, sys
sys.path.insert(0, "tests/unit")
def test_x(weo):
    df = weo[0]
    inh = df.filter(pl.col("flags").str.json_path_match("$.reporting_year_from").is_not_null())
    print("\nINHERITED", inh.height, inh.select("provider_geo","indicator").unique().head(10).rows())
