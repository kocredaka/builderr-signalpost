# Website evidence schema — deepening WAVE-3A (AUDIT §4 #12–#16)

Semua hasil pendalaman situs masuk ke dalam **website evidence value**
(`profile["evidence"]["website"]["value"]`). Adapter kontrak generik
(`contract.py`, milik worker core) mengubah tiap field value menjadi klaim
tanpa perubahan adapter; klaim situs menjadi `ambiguous` bila identity gate
tidak publishable (skor < 0,9), dan `available` bila publishable. Tidak ada
kode di sini yang mengubah ambang gate atau adapter.

Sumber: situs perusahaan sendiri (company-owned / company-reported). Nilai
registry tidak pernah ditimpa oleh nilai situs.

## Anggaran crawl (jalur urllib, `website.fetch_website`)

- Homepage + maks **8 halaman sekunder** (`MAX_SECONDARY_PAGES`): maks 4 dari
  link prioritas homepage, sisanya diisi kandidat sitemap.
- Sitemap: maks 10 file (`MAX_SITEMAP_FILES`), maks 1 MB per file, index
  diikuti satu tingkat. Feed: maks 2 feed, maks 500 KB per feed.
- Selalu: same-registered-domain, robots.txt dihormati, SSRF guard
  (`assert_public_url`) tidak dilonggarkan, batas byte dipatuhi. URL sitemap
  / feed di luar domain ditolak dan dihitung, tidak pernah di-fetch.

## Kunci baru di website evidence value

| Kunci | Isi |
|---|---|
| `site_contacts` | Kontak company-reported: `telephone[]`, `email[]`, `addresses[]` (kunci schema.org + `formatted`), `opening_hours[]`, `opening_hours_specifications[]`, `contact_points[]`, `provenance` (per field: `structured_data` / `footer`), `source`, `source_url`. Digabung dari structured data semua halaman yang di-crawl + footer homepage (jalur urllib). |
| `site_contact_comparison` | `registry_contacts_supplied` + `comparisons[]` per field (`email`, `telephone`, `address`): `site_values`, `registry_values`, `verdict` (`match` / `differs` / `site_only` / `registry_only` / `both_missing`). Cocok = koroborasi; beda = kedua nilai ditampilkan berlabel sumber. Perbandingan hanya jalan bila konteks registry dipasok (lihat di bawah). |
| `site_jobs` | Lowongan **terverifikasi**: observasi `job_posting` (`permitted_public_page` / `approved`, `company_owned`) dengan `title`, `date_posted`, `valid_through`, `expired` + `status` (`open` / `expired` / `unknown`), `job_location`, `hiring_organization`, `source_url`, `content_sha256`, `retrieved_at`, `exact_entity: true`, `identity_proof`. Lowongan kedaluwarsa ditandai, tidak dihapus. |
| `site_jobs_unverified` | Kandidat lowongan **ambigu** (karantina, jangan dipublikasi): bentuk sama, `exact_entity: false`, `verification_status: "ambiguous"`, `verification_reason` (`identity_context_not_supplied` / `hiring_organization_unmatched`). |
| `site_news_items` | Daftar item aktivitas bertanggal: `url`, `content_sha256`, `hash_scope` (`page_bytes` / `feed_entry`), `title`, `date_published` (null bila tak dikenal — tidak ditebak dari tanggal ambil), `date_source`, `page_kind` (`article` / `archive`), `origin` (`page` / `feed`), `source_label: "company_owned"`, `interpretation` ("bukan sentimen independen"), `retrieved_at`. Terbaru dulu, tanpa tanggal terakhir, maks 25. |
| `site_declared_facts` | Fakta company-reported dari structured data: `founding_date`, `number_of_employees_site_declared`, `aggregate_rating_site_declared` (berlabel "dideklarasikan situs, bukan review independen"), `vat_ids`. Hanya kunci yang ditemukan yang ada. Tampil berdampingan dengan registry, jangan disamakan. |
| `sitemap_discovery` | `sitemap_urls_checked[]`, `sitemap_urls_rejected_out_of_domain`, `urls_found`, `candidates_selected[]`, `errors[]`. |
| `feed_discovery` | `feed_urls[]`, `feeds_fetched`, `items_found`, `errors[]`. |
| `pages[]` entri | Bertambah: `published_date`, `date_source`, `page_kind` (`homepage` / `career` / `article` / `archive` / `contact` / `about` / `other`), `structured_types[]`, `structured_organisations[]`, `job_posting_candidates[]` (mentah, sebelum verifikasi). |

`structured_organisations` (level value) tetap dari homepage saja agar identity
gate melihat bukti yang sama seperti sebelum WAVE-3A — tetapi ekstraksinya
diperluas (#16): JSON-LD termasuk `@graph`, turunan `LocalBusiness`,
microdata, dan RDFa (dinormalisasi ke bentuk JSON-LD, ditandai
`_source_syntax`).

## Verifikasi lowongan (#14)

Ter verifikasi bila SALAH SATU terpenuhi: nama `hiringOrganization` cocok
dengan nama legal; identifier/nama hiring org memuat nomor organisasi;
URL hiring org berada di registered domain yang sama; atau nomor organisasi
muncul di teks halaman posting. Selain itu → `site_jobs_unverified`.

## Konteks identitas (permintaan ke koordinator)

`fetch_website()` menerima kwargs opsional `legal_name`,
`organisation_number`, `registry_contacts`. **Batch runner saat ini
(`scripts/run_competition_batch.py`, di luar kepemilikan WAVE-3A) belum
memasoknya**, sehingga pada run default: lowongan hanya terverifikasi lewat
kecocokan domain hiring-org URL, dan `site_contact_comparison` melaporkan
`registry_contacts_supplied: false`. Jalur Scrapy (`crawl_events.py`)
sudah memasok konteks dari profil, sehingga verifikasi + perbandingan
aktif di sana. Permintaan: satu perubahan kecil di `enrich()` batch runner
untuk meneruskan `profile["name"]`, `profile["organisation_number"]`, dan
kontak registry (via `site_extract.registry_contacts_from_profile`).

## Jalur Scrapy

`crawl_events.extract_page_event` / `merge_profile_events` menghasilkan
kunci yang sama. Perbedaan yang didokumentasikan: kontak footer tidak
tersedia di merge (tanpa soup yang ter-parse), dan sitemap/feed discovery
tidak dilakukan jalur Scrapy (`sitemap_discovery.status =
"not_performed_in_scrapy_path"`); scheduler Scrapy hanya mengikuti
priority links. Penambahan sitemap discovery ke `scrapy_crawler.py`
butuh perubahan di luar kepemilikan WAVE-3A.

## Prioritas halaman

`PRIORITY_TERMS` kini tinggal di `site_extract.py` (satu sumber) dan
di-re-export oleh `website.py`. Istilah asli kit mempertahankan urutan/rank;
ditambahkan: `karriere`, `ledige-stillinger`, `stillinger`, `jobb`,
`presse`, `blog`, `ansatte`, `apningstider`, `åpningstider`, `artikler`,
`careers`.

## Keterbatasan yang diketahui

- Situs JS-heavy tetap `js_fallback_candidate` tanpa fallback browser
  (keterbatasan kit yang sudah ada; tidak diubah di sini).
- Tanggal halaman hanya dari bukti eksplisit (JSON-LD/meta/URL/teks
  bertanggal penuh). Tanggal relatif ("i går", "for 2 dager siden") tidak
  di-parse → null.
- Klasifikasi arsip vs artikel memakai heuristik bentuk URL + tipe
  Article terstruktur; situs dengan URL non-standar bisa salah klasifikasi.
- Ekstraksi link halaman karier hanya menghasilkan kandidat tanpa tanggal /
  hiring org; verifikasinya bergantung pada nomor organisasi di halaman.
- `fetch_website()` gagal total di sandbox ini karena DNS resolver sandbox
  mengembalikan alamat CGNAT (`198.18.x.x`, non-global) sehingga SSRF guard
  memblokir — guard TIDAK dilonggarkan. Verifikasi dilakukan lewat
  mock/fixture (33 test) + probe parser atas byte HTML asli equinor.com
  (lihat laporan WAVE-3A). Di mesin bersih dengan DNS normal, guard
  berperilaku seperti semula.
