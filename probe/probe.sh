set +e
for h in www.gov.pl gov.pl api.sejm.gov.pl; do echo "== $h"; getent ahosts $h | head -4; curl -4 -sS -o /dev/null --max-time 30 -w "ipv4 %{http_code} %{remote_ip}
" https://$h/ ; curl -6 -sS -o /dev/null --max-time 15 -w "ipv6 %{http_code}
" https://$h/ ; done
curl -4 -sS -o /dev/null --max-time 30 -w "ot-poznan ipv4 %{http_code} %{size_download}
" -A "Mozilla/5.0" https://www.gov.pl/web/kowr/ot-poznan
