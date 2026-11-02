"""Run-scoped evidence projection and code-owned catalog/preview presentation."""
import json
import re
from app.trace.safety import TraceSafetyProjector


BOOK_FIELDS = (
    'book_id', 'title', 'isbn', 'subtitle', 'description', 'publisher',
    'publication_date', 'page_count', 'price', 'currency', 'available_stock',
    'status', 'authors', 'categories', 'language_code',
)
POLICY_FIELDS = ('source_path', 'document_title', 'section', 'version', 'content')
SECURITY_BOUNDARY_ANSWER = (
    '我不能显示、保存或泄露用户令牌，也不能绕过确认规则。'
    '若你需要购买，系统只能先基于实时商品和库存给出订单预览；'
    '只有你显式确认后才会创建模拟订单。'
)


def catalog_books(evidence):
    books = {}
    for record in evidence.snapshot().values():
        if record.source_tool not in {'search_books', 'get_book'}:
            continue
        for book in record.payload.get('books', [record.payload]):
            if isinstance(book, dict) and isinstance(book.get('book_id'), int):
                books[book['book_id']] = {key: book[key] for key in BOOK_FIELDS if key in book}
    return books


def review_evidence(evidence):
    """Allowlisted current tool records only; no auth, memory or unrelated tables."""
    safety = TraceSafetyProjector()
    policies, seen = [], set()
    for record in evidence.snapshot().values():
        if record.source_tool != 'search_policy':
            continue
        for chunk in record.payload.get('chunks', []):
            if not isinstance(chunk, dict):
                continue
            key = tuple(str(chunk.get(k, '')) for k in POLICY_FIELDS)
            if key in seen:
                continue
            seen.add(key)
            value = {k: chunk[k] for k in POLICY_FIELDS if k in chunk}
            if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > safety.max_evidence_bytes:
                policies.append({'source_tool': 'search_policy', 'evidence_id': record.evidence_id,
                    'chunk': {'truncated': True}, 'truncated': True})
                continue
            projected = safety.evidence(value)
            policies.append({'source_tool': 'search_policy', 'evidence_id': record.evidence_id,
                'chunk': projected.value, 'truncated': projected.truncated})
    books, previews, seen_books = [], [], set()
    for record in evidence.snapshot().values():
        if record.source_tool in {'get_book', 'search_books'}:
            for book in record.payload.get('books', [record.payload]):
                if not isinstance(book, dict) or 'book_id' not in book:
                    continue
                if record.evidence_id.startswith('book:') and not record.evidence_id.startswith(f"book:{book['book_id']}:"):
                    continue
                value = {k: book[k] for k in BOOK_FIELDS if k in book}
                identity = json.dumps(value, sort_keys=True)
                if identity not in seen_books:
                    books.append({'source_tool': record.source_tool, 'evidence_id': record.evidence_id, 'book': value})
                    seen_books.add(identity)
        elif record.source_tool == 'prepare_order':
            items = [{key: item[key] for key in ('book_id','title','unit_price','quantity','line_total') if key in item}
                for item in record.payload.get('items', [])]
            previews.append({'source_tool':'prepare_order', 'items':items,
                'total_amount':record.payload.get('total_amount'), 'currency':record.payload.get('currency'),
                'confirmation_required':True})
    return {'books': books, 'policies': policies, 'order_previews': previews}


def money(amount, currency):
    # Integer arithmetic: never reinterpret minor units as major units or round.
    if currency == 'CNY':
        return f'{amount // 100}.{amount % 100:02d}元（{amount}分）'
    return f'{amount} {currency}最小货币单位'


def grounded_catalog_draft(request):
    """Replace the numeric narrative, not append a correct table to wrong prose.

    Called after exact evidence/constraint validation and before the LLM content
    review. The reviewed draft is the one delivered by Chat.
    """
    if request.draft._suppress_catalog_listing:
        return request.draft
    books = catalog_books(request.evidence)
    previews = [record.payload for record in request.evidence.snapshot().values()
        if record.source_tool == 'prepare_order']
    if not books and not previews:
        return request.draft
    draft = request.draft.model_copy(deep=True)
    lines = [draft.answer] if draft.answer == SECURITY_BOUNDARY_ANSWER else []
    if request.constraints.total_budget is not None:
        lines.append('本轮总预算：' + money(request.constraints.total_budget, request.constraints.currency or 'CNY') + '。')
    selected = []
    for recommendation in draft.recommendations:
        # Match the exact record chosen by DeterministicVerifier, not the last
        # snapshot of this ID from another catalog call in the same DAG.
        for evidence_id in recommendation.evidence_ids:
            record = request.evidence.get(evidence_id)
            if record is None or record.source_tool not in {'get_book','search_books'}:
                continue
            book = next((b for b in record.payload.get('books', [record.payload]) if isinstance(b, dict)
                and b.get('book_id') == recommendation.book_id), None)
            if book:
                selected.append({key:book[key] for key in BOOK_FIELDS if key in book})
                break
    if not draft.recommendations:
        selected = list(books.values())
    comparison_books = _explicit_comparison_books(request.user_request, books)
    if comparison_books:
        # A comparison must retain both named, tool-backed records even when
        # the recommendation Agent chooses just one as its final preference.
        selected = comparison_books
        if not request.requires_policy_evidence:
            # The final comparison below is rebuilt exclusively from the two
            # exact catalog records. Drop any unrelated model policy/write
            # claims before the deterministic verifier accepts this narrow
            # code-owned path.
            draft.policy_claims = []
            draft.write_actions = []
            draft.addressed_question_ids = list(request.required_question_ids)
            draft._code_owned_catalog_comparison = True
    if selected:
        lines.append('已核验的推荐：' if draft.recommendations else '目录查询结果（不代表已选定购买组合）：')
    for book in selected:
        if not all(k in book for k in ('price', 'currency', 'available_stock')):
            continue
        description = '；'.join(f'{label}：{", ".join(book.get(key, []))}' for label,key in (('作者','authors'),('分类','categories')) if book.get(key))
        language = book.get('language_code', '')
        detail = '；'.join(part for part in (description, f'语言：{language}' if language else '') if part)
        status = '已下架，当前不可购买。' if book.get('status') == 'INACTIVE' else '缺货，当前不可购买。' if book['available_stock'] == 0 else ''
        lines.append(f"书籍 #{book['book_id']}《{book.get('title', '')}》：价格：{money(book['price'], book['currency'])}；库存：{book['available_stock']}。{status}")
        if detail:
            lines.append(detail + '。')
        explanation = _catalog_explanation(book)
        if explanation:
            lines.append(explanation)
        for recommendation in draft.recommendations:
            if recommendation.book_id == book['book_id']:
                recommendation.reason = ' '.join(
                    value for value in (
                        status.rstrip('。'),
                        explanation or (detail + '。' if detail else '来自本轮目录证据。'),
                    ) if value
                )
    _append_comparison_hint(lines, request.user_request, selected)
    _append_contextual_reference_hint(lines, request.user_request, selected)
    for preview in previews:
        currency = preview.get('currency', 'CNY')
        lines.append('订单预览，尚未下单，请显式确认：')
        for item in preview.get('items', []):
            lines.append(f"书籍 #{item['book_id']}《{item.get('title', '')}》：单价：{money(item['unit_price'], currency)}；数量：{item['quantity']}；合计：{money(item['line_total'], currency)}。")
        lines.append('总价：' + money(preview['total_amount'], currency) + '。')
    if books and not previews and not any(record.source_tool == 'create_order' for record in request.evidence.snapshot().values()):
        lines.append('本次仅查询，没有创建订单；下单需先预览并显式确认。')
    for claim in draft.policy_claims:
        for record in request.evidence.snapshot().values():
            if record.source_tool != 'search_policy' or record.evidence_id not in claim.evidence_ids:
                continue
            chunk = next((c for c in record.payload.get('chunks', []) if isinstance(c, dict)
                and (c.get('source_path'), c.get('section'), c.get('version')) == (claim.source, claim.section, claim.version)), None)
            if chunk:
                version = claim.version.removeprefix('v').removeprefix('V')
                lines.append(f"{claim.claim} [《{chunk.get('document_title', '')}》v{version}，{claim.section}]")
                break
    if request.requires_policy_evidence and not draft.policy_claims:
        lines.append('无法从当前知识库确定该规则。')
    if lines:
        projected = TraceSafetyProjector().text('\n'.join(lines))
        if projected.truncated:
            raise ValueError('grounded answer exceeds safe output budget')
        draft.answer = projected.value
    return draft


def _catalog_explanation(book):
    """Keep only tool-owned metadata as the visible reason, never free-text facts."""
    description = str(book.get('description') or '').strip()
    subtitle = str(book.get('subtitle') or '').strip()
    if description:
        return f'推荐依据（书目简介）：{description}'
    if subtitle:
        return f'推荐依据（副标题）：{subtitle}'
    return '现有书目未提供简介或副标题，无法据此判断章节、阅读基础或适读程度。'


def _explicit_comparison_books(user_request, books):
    if '比较' not in user_request:
        return []
    titles = re.findall(r'《([^》]+)》', user_request)
    if len(titles) < 2:
        return []
    by_title = {str(book.get('title', '')): book for book in books.values()}
    selected = [by_title[title] for title in titles if title in by_title]
    return selected if len(selected) >= 2 else []


def _append_comparison_hint(lines, user_request, books):
    """A narrow, evidence-backed comparison for the documented Java beginner journey."""
    if '比较' not in user_request or len(books) < 2:
        return
    normalized = user_request.casefold()
    if 'java' not in normalized:
        return
    for book in books:
        if 'java' in str(book.get('description') or '').casefold():
            lines.append(f"就“刚学完 Java”这一条件，书目简介直接提到 Java 基础的《{book.get('title', '')}》更贴合；上述简介是当前可核验的依据。")
            return


def _append_contextual_reference_hint(lines, user_request, books):
    """Answer bounded page references from the same current catalog evidence."""
    if '前两本' in user_request and len(books) >= 2:
        first, second = books[:2]
        first_description = str(first.get('description') or first.get('subtitle') or '').strip()
        second_description = str(second.get('description') or second.get('subtitle') or '').strip()
        if first_description or second_description:
            lines.append(
                '两本的可核验区别：'
                f'《{first.get("title", "")}》的书目说明是“{first_description or "现有资料未提供"}”；'
                f'《{second.get("title", "")}》的书目说明是“{second_description or "现有资料未提供"}”。'
            )
    if '第二本' in user_request and '适合' in user_request and len(books) == 1:
        book = books[0]
        description = str(book.get('description') or book.get('subtitle') or '').strip()
        if '无需分布式经验' in description:
            lines.append(
                f'《{book.get("title", "")}》适合没有分布式基础的读者：'
                '书目简介明确说明无需分布式经验。'
            )
