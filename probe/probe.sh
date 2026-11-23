set -e
curl -sS -I --max-time 60 https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/wynik_opolskie.zip | head -5
curl -sS -o /tmp/o.zip --max-time 900 -w "%{size_download} bytes in %{time_total}s
" https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/wynik_opolskie.zip
curl -sS -I --max-time 30 https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N50_00_E022_00_DEM/Copernicus_DSM_COG_10_N50_00_E022_00_DEM.tif | head -1
