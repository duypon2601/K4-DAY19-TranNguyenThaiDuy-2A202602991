# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Trần Nguyễn Thái Duy  **MSSV:** 2A202602991  **Ngày:** 05-10-2026

> Số liệu lấy từ `ket_qua_benchmark_kg.txt` (ontology tự thiết kế, xem `report/ONTOLOGY.md`). Số liệu của ontology gợi ý, dùng để so sánh, lấy từ `ket_qua_benchmark_kg.hint.txt`. Cả hai chạy với `gemini-3.1-flash-lite` + `gemini-embedding-001`, `top_k=3`, `chunk_size=800`, 176 chunk.

## 1. Chi phí (10 điểm)

```
Chat model: gemini:gemini-3.1-flash-lite | Embedding: gemini:gemini-embedding-001 | top_k=3 | chunk_size=800 | chunks=176 | KG: 242 nodes / 523 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    100.1
graph       196     39639     6603   0.01981    183.7

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.33      696       70   0.00028     5.15
graph       0.89   2.00     2447      139   0.00082     5.82
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0,00000 (*) | 0,01981 | không tính được tỉ lệ (*) |
| Indexing giây | 100,1 | 183,7 | ×1,84 |
| Mỗi câu: USD | 0,00028 | 0,00082 | ×2,9 |
| Mỗi câu: giây | 5,15 | 5,82 | ×1,13 |
| Mỗi câu: in_tok | 696 | 2.447 | ×3,5 |

(*) Endpoint embedding của Gemini không trả số token và `src/llm.py` không có giá cho `gemini-embedding-001`, nên chi phí embedding được ghi là 0 ở **cả hai** pipeline. Con số 0,01981 USD vì vậy là riêng phần dựng KG (20 lần gọi LLM), không phải tổng chi phí thật. Giá chat tôi thêm vào `PRICES_PER_M` theo trang giá Gemini: 0,25 / 1,50 USD cho 1 triệu token vào/ra.

**Chi phí tăng thêm đến từ đâu?**
> Lúc dựng: toàn bộ phần tăng là 20 lần gọi LLM trích xuất tin tức (39.639 token vào, 6.603 token ra, 83,6 giây = 183,7 − 100,1); phần luật dùng regex nên tốn 0 token. Lúc hỏi: token vào tăng ×3,5 vì prompt có thêm dữ kiện graph (trung bình +1.751 token/câu), và token ra tăng gấp đôi (70 → 139) vì câu trả lời có thêm Điều luật. Độ trễ chỉ tăng ×1,13 vì truy vấn Cypher chỉ mất vài chục mili giây, phần lớn thời gian vẫn là LLM.
>
> Ước tính: mỗi câu GraphRAG đắt hơn 0,00054 USD. Chi phí dựng KG (0,01981 USD) bằng chi phí tăng thêm của khoảng 37 câu hỏi, nên sau vài chục câu thì chi phí hỏi mới là phần chính, không phải chi phí dựng. GraphRAG không bao giờ "hòa vốn" về tiền so với Flat RAG; thứ mua được là độ chính xác ở mục 2.
>
> Lưu ý về độ trễ: key Gemini free-tier giới hạn 100 lệnh embedding/phút, SDK tự chờ và thử lại khi bị 429, nên cột `seconds` có nhiễu (ví dụ Q1 graph 14,35 giây, Q4 flat 10,04 giây). Thời gian indexing của Flat (100,1 giây cho 176 chunk) chủ yếu là thời gian chờ quota.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1,00 / 2 | 1,00 / 2 | Hòa | Đáp án nằm gọn trong một khoản của Điều 2 Luật PCMT, vector search lấy đúng chunk; graph không thêm gì ngoài số Điều. |
| Q2 | single-hop-news | 1,00 / 2 | 1,00 / 2 | Hòa | Tên hai bị cáo và mức án cùng nằm trong một bài báo; graph chỉ thêm "Điều 251". |
| Q3 | cross-kb | 0,33 / 1 | 1,00 / 2 | Graph | Flat chỉ lấy được chunk tin tức nên thiếu Điều 251 và khung 02–07 năm; graph đi Person → Charge → Crime → Article. |
| Q4 | cross-kb | 0,33 / 1 | 1,00 / 2 | Graph | Biệt danh "Hoàng Nato" khớp `Person.aliases`, rồi `Article.max_penalty` cho khung cao nhất mà không chunk tin tức nào có. |
| Q5 | cross-kb-multi-hop | 0,40 / 1 | 1,00 / 2 | Graph | Cần so 9,6kg MDMA với ngưỡng trong khoản 4 Điều 250; graph tính sẵn bằng cạnh `HAS_THRESHOLD`. |
| Q6 | aggregation | 0,00 / 1 | 0,33 / 2 | Graph (theo judge) | Top-3 chunk không thể phủ mọi vụ có MDMA; graph liệt kê từ `Substance ← INVOLVES`. Recall thấp là do phép đo (mục 3, E4). |

**Quy luật:** khi đáp án nằm trong một tài liệu (Q1, Q2), hai pipeline hòa và graph chỉ làm tốn thêm token. Khi đáp án cần ghép 2 KB (Q3–Q5) hoặc gom nhiều tài liệu (Q6), Flat dừng ở recall 0,00–0,40 và đều nói thẳng "ngữ cảnh không cung cấp", còn graph đạt judge 2 ở cả bốn câu. Trên 3 câu `cross-kb`, recall trung bình là 0,35 (Flat) so với 1,00 (Graph).

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật (tìm thấy ở ontology gợi ý, đã sửa bằng thiết kế)

- **Hiện tượng:** với ontology gợi ý, Q4 hỏi mức phạt tù **tối đa** nhưng GraphRAG trả lời không đủ thông tin, dù graph có đủ 4 khoản của Điều 255.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q4 pipeline graph, recall=0,67 judge=1:

```
- **Mức phạt tù tối đa:** Theo **Điều 255 Bộ luật Hình sự (Tội tổ chức sử dụng trái phép chất ma túy)**,
ngữ cảnh chỉ cung cấp thông tin về khoản 1 (phạt tù từ 02 năm đến 07 năm). Đối với các khoản cao hơn
(khoản 2, 3, 4) của Điều 255, ngữ cảnh không cung cấp nội dung chi tiết, do đó không đủ thông tin để
xác định mức phạt tù tối đa
```

  Điều 255 không có khoản nào nêu tên chất hay ngưỡng khối lượng, nên điều kiện "khoản nhắc tới chất của vụ" không bao giờ đúng với nó (truy vấn chạy trên graph của tôi; Điều 250 để so sánh có 56 cạnh `HAS_THRESHOLD`):

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl)
RETURN cl.number AS khoan, COUNT { (cl)-[:HAS_THRESHOLD]->() } AS nguong, cl.penalty AS penalty ORDER BY khoan
```

```
{"khoan": 1, "nguong": 0, "penalty": "phạt tù từ 02 năm đến 07 năm"}
{"khoan": 2, "nguong": 0, "penalty": "phạt tù từ 07 năm đến 15 năm"}
{"khoan": 3, "nguong": 0, "penalty": "phạt tù từ 15 năm đến 20 năm"}
{"khoan": 4, "nguong": 0, "penalty": "phạt tù 20 năm hoặc tù chung thân"}
{"khoan": 5, "nguong": 0, "penalty": "phạt tiền từ 50.000.000 đồng đến 500.000.000 đồng, phạt quản chế, …"}
```

- **Nguyên nhân:** nằm ở **Cypher của KG-3** (quy tắc lọc khoản ở LAB_GUIDE Bước 5), và sâu hơn là ở **thiết kế ontology**: quy tắc "khoản 1 + khoản nhắc chất của vụ" ngầm giả định mọi tội đều định khung theo khối lượng. Tội tổ chức sử dụng định khung theo tình tiết (số người, độ tuổi, hậu quả), nên chỉ còn lại khoản 1. LLM đã làm đúng: nó từ chối bịa.
- **Đề xuất sửa (đã làm):** thêm `Article.max_clause` và `Article.max_penalty`, tính bằng regex lúc dựng graph (`penalty_rank` trong `src/graph.py`: tử hình > chung thân > số năm lớn nhất, bỏ khoản hình phạt bổ sung). `context()` gửi một dòng mỗi Điều gồm khung của mọi khoản và khung cao nhất:

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'}) RETURN a.max_clause, a.max_penalty
```

```
{"max_clause": 4, "max_penalty": "phạt tù 20 năm hoặc tù chung thân"}
```

  Kết quả ở `ket_qua_benchmark_kg.txt`, Q4 graph, recall=1,00 judge=2: *"khung hình phạt cao nhất cho hành vi này là khoản 4, với mức phạt tù tối đa là 20 năm hoặc tù chung thân"*. Đánh đổi: 0 token lúc dựng; lúc hỏi mỗi Điều tốn một dòng khoảng 80–100 token, vẫn rẻ hơn nhiều so với gửi nguyên văn mọi khoản (in_tok trung bình giảm từ 5.606 xuống 2.447).

### Lỗi E4: Phép đo sai (recall và judge mâu thuẫn ở Q6)

- **Hiện tượng:** Q6 pipeline graph có `recall=0.33` nhưng `judge=2`. Ngược chiều, Q6 flat có `recall=0.00` nhưng `judge=1`. Ở lần chạy ontology gợi ý, cùng câu này graph được `recall=1.00`. Nhìn riêng cột recall sẽ kết luận ontology mới làm Q6 tệ đi.
- **Bằng chứng:** `ket_qua_benchmark_kg.txt`, Q6 graph (rút gọn, giữ nguyên tên vụ):

```
1. Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam qua sân bay Nội Bài: hơn 9,6kg MDMA … đối tượng Huy và Đạt
2. Vụ góp tiền mua ma túy tại Hà Nội: 5 viên MDMA
3. Vụ triệt phá 8 đường dây ma túy tại TP.HCM: Liên quan đến MDMA
4. Vụ án sai phạm tại Viện Pháp y tâm thần Trung ương: 0,686g MDMA
```

  `must_include` của Q6 trong `data/benchmark_kg.json` là `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`. Câu trả lời nêu đúng cả ba vụ của đáp án chuẩn nhưng gọi vụ bằng tên vụ ("đối tượng Huy", "Vụ góp tiền mua ma túy tại Hà Nội") thay vì họ tên đầy đủ, nên chỉ khớp 1/3 từ khóa. Cypher trả lời thẳng câu hỏi cho đúng 4 vụ đó:

```cypher
MATCH (k:Case)-[:INVOLVES]->(:Substance {name:'MDMA'})
RETURN k.name AS vu, [(p:Person)-[i:INVOLVED_IN]->(k) WHERE i.role IN ['bị cáo','bị can','nghi phạm'] | p.name][..4] AS nguoi
```

```
Vụ án sai phạm tại Viện Pháp y tâm thần Trung ương | Trần Quốc An, Ngô Việt Dũng, Cao Thị Bích Hằng, Lê Văn Đông
Vụ triệt phá 8 đường dây ma túy tại TP.HCM        | Phạm Minh Sang, Lê Đăng Khoa, Giang Quốc Khánh, Phạm Phương Anh
Vụ góp tiền mua ma túy tại Hà Nội                 | Nguyễn Quang Hưng, Kim Xuân Tuấn, Trịnh Vũ Kiên, Lê Minh Thành
Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam… | Nguyễn Tiến Đạt, Cái Quang Huy
```

  Vụ thứ 4 (8 đường dây) không có trong đáp án chuẩn nhưng là đúng: bài `news-100260920221957595` viết *"hoạt động mua bán ma túy loại etomidate, ketamine, thuốc lắc"*, và "thuốc lắc" là MDMA (kiểm bằng `grep -i "thuốc lắc" data/drug_news/*.md`).
- **Nguyên nhân:** nằm ở **phép đo**, hai tầng. (1) `keyword_recall` so chuỗi con nguyên văn, nên phạt câu trả lời đúng nhưng diễn đạt khác; với câu `aggregation`, tên người chỉ là một cách gọi tên vụ. (2) Đáp án chuẩn (`gold`) thiếu một vụ vì người soạn tìm theo chữ "MDMA", còn bài báo viết "thuốc lắc"; LLM judge chấm 2 vì ba ý chính đều có, nhưng một judge khắt khe hơn có thể trừ điểm vì "thừa". Ở đây **judge đúng, recall sai**. Ở chiều ngược lại (flat: recall 0,00, judge 1) thì judge lại dễ dãi: Flat liệt kê 3 đoạn văn bản nhưng 2 trong 3 đoạn thuộc cùng một vụ.
- **Đề xuất sửa:** trong `data/benchmark_kg.json`, cho mỗi từ khóa một danh sách cách viết tương đương (ví dụ `["Cái Quang Huy", "sân bay Nội Bài"]`) và tính khớp khi có một trong số đó; bổ sung vụ 8 đường dây vào `gold`. Với câu `aggregation` nên chấm bằng precision/recall trên **tập vụ việc** thay vì từ khóa. Đánh đổi: tốn công gán nhãn, và file benchmark là của đề nên tôi không sửa, chỉ ghi nhận. Phía pipeline, tôi đã đưa tên người bị buộc tội vào dữ kiện của từng vụ, nhưng LLM vẫn chọn gọi theo tên vụ; ép nó nêu tên người chỉ để khớp từ khóa là tối ưu theo thước đo chứ không phải theo chất lượng.

### Lỗi E3: Trùng thực thể (trước/sau, và phần còn sót)

- **Hiện tượng:** ở ontology gợi ý, một vụ ngoài đời thành nhiều node `Case`; câu trả lời Q6 của gợi ý vì vậy liệt kê vụ Cái Quang Huy ba lần (mục 1, 3, 6 trong `ket_qua_benchmark_kg.hint.txt`).
- **Bằng chứng:** truy vấn trên graph gợi ý:

```cypher
MATCH (p:Person)-[:INVOLVED_IN]->(k) WITH p, count(k) AS n WHERE n>1 RETURN p.name, n
```

```
Dương Minh Tuấn 4 | Phan Kim Nhi 3 | Cái Quang Huy 2 | Nguyễn Minh Đức 2 | Nguyễn Thị Mai Anh 2 | Lê Văn Đông 2 | Trần Quốc An 2
```

  Bốn `Case` của Dương Minh Tuấn mang bốn tên khác nhau do LLM đặt ("Vụ bắt giữ 126 người liên quan 8 đường dây ma túy tại TP.HCM", "Vụ triệt phá 8 đường dây ma túy tại TP.HCM liên quan TikToker Phannhibeauty và Hoàng Nato", …). Cùng truy vấn trên graph của tôi trả về 0 dòng; số `Case` giảm từ 17 xuống 10.
- **Nguyên nhân:** **thiết kế ontology**: `MERGE (k:Case {name})` dùng tên do LLM tự đặt làm khóa, mà mỗi bài báo được trích xuất độc lập nên không có gì bảo đảm hai bài đặt cùng tên.
- **Đề xuất sửa (đã làm) và phần còn sót:** `resolve_cases` gộp các vụ có chung người có tên, khóa `Case.id` sinh trong code. Còn sót: (1) hai vụ ở Phú Quốc không có người nêu tên nên không có gì để gộp hay phân biệt ngoài tên; (2) node `Substance {name:'tinh thể rắn màu trắng'}` là mô tả chứ không phải tên chất. Sửa (2) cần thêm quy tắc "chỉ nhận tên chất có trong danh mục hoặc dạng tên hóa học", đổi lại sẽ mất các chất mới như etomidate.

### Lỗi E6: Thuộc tính thiếu (ngắn)

- **Hiện tượng / bằng chứng:**

```cypher
MATCH (p:Person)-[i:INVOLVED_IN]->(k:Case) WHERE NOT (p)-[:FACES]->(:Charge)-[:IN_CASE]->(k)
RETURN p.name, i.role, k.name
```

```
Ngô Việt Dũng | bị cáo | Vụ án sai phạm tại Viện Pháp y tâm thần Trung ương
Cao Thị Bích Hằng | bị cáo | (cùng vụ)
Ngô Văn Vinh | cán bộ | (cùng vụ)        Trần Văn Trường | cán bộ | (cùng vụ)
Nguyễn Minh Nhân | bị can | Vụ chống người thi hành công vụ tại An Giang
Trần Ngọc Nam | cán bộ | (cùng vụ)
```

- **Nguyên nhân:** ba loại khác nhau. *Hợp lý:* cán bộ không bị buộc tội; Nguyễn Minh Nhân bị khởi tố tội chống người thi hành công vụ, không thuộc 13 tội danh của KB luật nên `link_crime` trả `None` đúng như thiết kế. *Lỗi trích xuất:* bài `news-100260930085028036` chỉ kể Ngô Việt Dũng và Cao Thị Bích Hằng có mặt, không nêu tội danh của họ, nhưng LLM vẫn gán `role = "bị cáo"`: vai trò là suy đoán, tội danh thì để trống.
- **Đề xuất sửa:** trong prompt, yêu cầu trích kèm câu gốc làm căn cứ cho `role`, và hạ về "người liên quan" khi không có căn cứ. Đánh đổi: thêm khoảng 20–30% token đầu ra lúc dựng graph.

## 4. Kết luận (5 điểm)

> **Flat RAG là đủ** khi đáp án nằm trong một tài liệu: Q1 và Q2 đều 1,00 / 2 ở cả hai pipeline, trong khi GraphRAG tốn gấp 3,5 lần token vào (2.447 so với 696) và gấp 2,9 lần tiền mỗi câu (0,00082 so với 0,00028 USD) mà không thêm điểm nào.
>
> **KG đáng tiền** khi (a) dữ liệu gồm nhiều nguồn có cấu trúc khác nhau nhưng chia sẻ một loại thực thể ổn định (ở đây là tội danh và chất ma túy), và (b) câu hỏi cần ghép các nguồn đó hoặc gom nhiều tài liệu. Trên 4 câu như vậy (Q3–Q6), Flat chỉ đạt judge 1 ở cả bốn câu, GraphRAG đạt judge 2 ở cả bốn; tính chung judge tăng từ 1,33 lên 2,00 và recall từ 0,51 lên 0,89, với độ trễ chỉ tăng ×1,13 (5,15 → 5,82 giây).
>
> Về tiền: dựng KG tốn 0,01981 USD một lần cho 20 bài báo (khoảng 0,001 USD/bài), mỗi câu hỏi đắt thêm 0,00054 USD. Với kho tài liệu thay đổi thường xuyên và ít câu hỏi, chi phí trích xuất lại sẽ lấn át; với kho ổn định và nhiều câu hỏi xuyên nguồn, phần dựng graph nhanh chóng trở nên không đáng kể.
>
> Hai điều tôi rút ra thêm: thiết kế ontology ảnh hưởng tới chi phí nhiều không kém độ chính xác (cùng model, cùng câu hỏi, đổi ontology giảm in_tok mỗi câu từ 5.606 xuống 2.447 và sửa được Q4); và con số tổng hợp có thể đánh lừa (recall của Q6 giảm từ 1,00 xuống 0,33 trong khi câu trả lời tốt hơn), nên phải đọc từng câu trả lời.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.02s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.1-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 158 node / 385 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 18 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00186. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (nghi phạm, giai đoạn truy tố, tội vận chuyển trái phép chất ma túy, Điều 250 BLHS).

Truy vấn đã đổi theo ontology của tôi:

```cypher
// kg_cross_kb.png (Q-B)
MATCH p=(:Person)-[:FACES]->(:Charge)-[:FOR_CRIME]->(:Crime)<-[:DEFINES]-(:Article) RETURN p LIMIT 25;

// kg_my_case.png (Q-D)
MATCH p=(:Person {name:'Cái Quang Huy'})-[:FACES]->(ch:Charge)-[:FOR_CRIME]->(:Crime)<-[:DEFINES]-(:Article)
MATCH q=(ch)-[:IN_CASE]->(k:Case) OPTIONAL MATCH r=(k)-[:INVOLVES|LOCATED_IN]->() RETURN p, q, r;
```

## Vấn đề gặp phải (không tính điểm)

> - **Model mặc định không dùng được.** `gemini-2.5-flash-lite` trả 404 *"no longer available to new users"*. Tôi đặt `GEMINI_CHAT_MODEL=gemini-3.1-flash-lite` trong `.env` và thêm giá của model này vào `PRICES_PER_M` trong `src/llm.py`.
> - **Quota free-tier của Gemini.** `429 … embed_content_free_tier_requests, limit: 100` làm `bench_kg.py --judge` dừng giữa chừng hai lần. Tôi đặt `max_retries=8` cho client trong `src/llm.py` để SDK tự chờ và thử lại; hệ quả là cột `seconds` có nhiễu như đã nêu ở mục 1.
> - **Chi phí embedding hiển thị bằng 0** vì endpoint tương thích OpenAI của Gemini không trả số token cho embedding và bảng giá của lab không có giá model này. Không sửa được từ phía bài làm.
> - **Ảnh chụp** là toàn bộ cửa sổ Chrome (có thanh địa chỉ `localhost:7474/browser/`, ô truy vấn và Results overview), chụp trên graph dựng lại bằng `python bench_kg.py --build` sau khi chạy benchmark. Graph dựng lại có đúng 242 node / 523 cạnh như dòng đầu của `ket_qua_benchmark_kg.txt`.
> - **File baseline** `ket_qua_benchmark_kg.hint.txt` được sinh trước khi tôi thêm `max_retries`; phần code ontology gợi ý không đổi và chạy lại được bằng `KG_ONTOLOGY=hint`.
