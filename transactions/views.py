import math

from django.db.models import Q
from django.utils import timezone
from rest_framework.views import APIView

from .models import Transaction, TransactionDuplicate
from core.responses import error_response, success_response
from .serializers import (
    DuplicateListQuerySerializer,
    DuplicateResolutionSerializer,
    BusinessScopeQuerySerializer,
    TransactionCategoryUpdateSerializer,
    TransactionDuplicateSerializer,
    TransactionListQuerySerializer,
    TransactionPurposeUpdateSerializer,
    TransactionSerializer,
    TransactionSyncRequestSerializer,
    TransactionSyncRetryRequestSerializer,
)
from .services.normalizers.helpers import TransactionNormalizationError
from .services.querysets import with_pending_duplicate_flag
from .services.sync_service import (
    NoPendingTwoWayAuthError,
    PendingTwoWayAuthError,
    TransactionSourceMismatchError,
    TransactionSyncService,
)


def _paginated_data(
    queryset,
    serializer_class,
    *,
    page,
    page_size,
    prepare_items=None,
):
    total_count = queryset.count()
    offset = (page - 1) * page_size
    items = list(queryset[offset : offset + page_size])
    if prepare_items is not None:
        prepare_items(items)
    return {
        "items": serializer_class(items, many=True).data,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": math.ceil(total_count / page_size),
        },
    }


def _business_scope(request):
    query = BusinessScopeQuerySerializer(data=request.query_params)
    if not query.is_valid():
        return None, error_response(
            code="INVALID_BUSINESS_SCOPE",
            message="business_id가 필요하거나 올바르지 않습니다.",
            errors=query.errors,
        )
    return query.validated_data["business"], None


def _mark_duplicate_relations(items):
    transaction_ids = {
        transaction_id
        for item in items
        for transaction_id in (
            item.primary_transaction_id,
            item.suspected_transaction_id,
        )
    }
    pending_ids = set()
    pending_pairs = TransactionDuplicate.objects.filter(
        Q(primary_transaction_id__in=transaction_ids)
        | Q(suspected_transaction_id__in=transaction_ids),
        status=TransactionDuplicate.Status.PENDING,
    ).values_list("primary_transaction_id", "suspected_transaction_id")
    for primary_id, suspected_id in pending_pairs:
        pending_ids.update((primary_id, suspected_id))

    for item in items:
        item.primary_transaction.has_pending_duplicate = (
            item.primary_transaction_id in pending_ids
        )
        item.suspected_transaction.has_pending_duplicate = (
            item.suspected_transaction_id in pending_ids
        )


class TransactionSyncView(APIView):
    def post(self, request):
        serializer = TransactionSyncRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                code="INVALID_TRANSACTION_SYNC_REQUEST",
                message="거래 동기화 요청이 올바르지 않습니다.",
                errors=serializer.errors,
            )

        params = serializer.validated_data
        from tax.services.closing_service import MonthlyCloseService

        if MonthlyCloseService.has_closed_month_between(
            business_id=params["business"].id,
            start_date=params["start_date"],
            end_date=params["end_date"],
        ):
            return error_response(
                code="MONTH_ALREADY_CLOSED",
                message="마감된 월과 겹치는 거래는 다시 동기화할 수 없습니다.",
                status=409,
            )
        try:
            result = TransactionSyncService().sync(
                business=params["business"],
                start_date=params["start_date"],
                end_date=params["end_date"],
                sources=params["sources"],
            )
        except PendingTwoWayAuthError as exc:
            return error_response(
                code="CODEF_TWO_WAY_AUTH_PENDING",
                message="이미 진행 중인 카카오 인증 요청이 있습니다.",
                errors={"detail": str(exc)},
                status=409,
            )
        except (TransactionNormalizationError, TransactionSourceMismatchError) as exc:
            return error_response(
                code="CODEF_TRANSACTION_DATA_ERROR",
                message="CODEF 거래 데이터를 처리하지 못했습니다.",
                errors={"detail": str(exc)},
                status=502,
            )
        except NotImplementedError as exc:
            return error_response(
                code="CODEF_TRANSACTION_SOURCE_UNAVAILABLE",
                message="현재 모드에서는 거래 데이터 소스를 사용할 수 없습니다.",
                errors={"detail": str(exc)},
                status=502,
            )

        if result["outcome"] == "AUTH_REQUIRED":
            return success_response(
                code="CODEF_TWO_WAY_AUTH_REQUIRED",
                message="카카오 추가인증이 필요합니다. 인증 후 재시도해주세요.",
                data=result,
            )

        return success_response(
            code="TRANSACTION_SYNC_SUCCESS",
            message="거래 데이터를 동기화했습니다.",
            data=result,
        )


class TransactionSyncRetryView(APIView):
    """카카오 2-way 인증 완료 후, 대기 중이던 거래 조회를 재개한다.

    business_id만 받는다 — 어떤 상품/기간을 재요청할지는
    CodefConnection.pending_*에 저장된 값으로 서버가 판단한다.
    """

    def post(self, request):
        serializer = TransactionSyncRetryRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                code="INVALID_TRANSACTION_SYNC_RETRY_REQUEST",
                message="거래 동기화 재시도 요청이 올바르지 않습니다.",
                errors=serializer.errors,
            )

        business = serializer.validated_data["business"]

        try:
            result = TransactionSyncService().retry(business)
        except NoPendingTwoWayAuthError as exc:
            return error_response(
                code="CODEF_NO_PENDING_TWO_WAY_AUTH",
                message="재시도할 카카오 추가인증 요청이 없습니다.",
                errors={"detail": str(exc)},
                status=409,
            )
        except (TransactionNormalizationError, TransactionSourceMismatchError) as exc:
            return error_response(
                code="CODEF_TRANSACTION_DATA_ERROR",
                message="CODEF 거래 데이터를 처리하지 못했습니다.",
                errors={"detail": str(exc)},
                status=502,
            )
        except NotImplementedError as exc:
            return error_response(
                code="CODEF_TRANSACTION_SOURCE_UNAVAILABLE",
                message="현재 모드에서는 거래 데이터 소스를 사용할 수 없습니다.",
                errors={"detail": str(exc)},
                status=502,
            )

        if result["outcome"] == "AUTH_REQUIRED":
            # N차 인증 케이스: 재시도했는데 또 추가인증이 필요한 경우.
            return success_response(
                code="CODEF_TWO_WAY_AUTH_REQUIRED",
                message="카카오 추가인증이 필요합니다. 인증 후 다시 재시도해주세요.",
                data=result,
            )

        return success_response(
            code="TRANSACTION_SYNC_RETRY_SUCCESS",
            message="거래 동기화를 재개했습니다.",
            data=result,
        )


class TransactionListView(APIView):
    def get(self, request, business_id=None):
        query_data = request.query_params.copy()
        if business_id is not None and "business_id" not in query_data:
            query_data["business_id"] = business_id
        query = TransactionListQuerySerializer(data=query_data)
        if not query.is_valid():
            return error_response(
                code="INVALID_TRANSACTION_QUERY",
                message="거래 목록 조회 조건이 올바르지 않습니다.",
                errors=query.errors,
            )

        params = query.validated_data
        queryset = with_pending_duplicate_flag(
            Transaction.objects.filter(business=params["business"])
        )
        if params.get("start_date"):
            queryset = queryset.filter(transaction_date__gte=params["start_date"])
        if params.get("end_date"):
            queryset = queryset.filter(transaction_date__lte=params["end_date"])
        for field in ["transaction_type", "source_type", "category", "expense_purpose"]:
            if params.get(field):
                queryset = queryset.filter(**{field: params[field]})

        data = _paginated_data(
            queryset,
            TransactionSerializer,
            page=params["page"],
            page_size=params["page_size"],
        )
        return success_response(
            code="TRANSACTION_LIST_SUCCESS",
            message="거래 목록을 조회했습니다.",
            data=data,
        )


class TransactionExportView(APIView):
    """지출 내역 분류 화면에서 거래 내역을 CSV 파일로 즉시 다운로드하는 API."""

    def get(self, request, business_id=None):
        import csv
        import io
        from django.http import HttpResponse
        from tax.services.periods import month_range

        query_data = request.query_params.copy()
        if business_id is not None and "business_id" not in query_data:
            query_data["business_id"] = business_id

        # year, month가 들어온 경우 start_date, end_date 자동 계산
        if "year" in query_data and "month" in query_data:
            try:
                y = int(query_data["year"])
                m = int(query_data["month"])
                s_date, e_date = month_range(y, m)
                if "start_date" not in query_data:
                    query_data["start_date"] = s_date.isoformat()
                if "end_date" not in query_data:
                    query_data["end_date"] = e_date.isoformat()
            except (ValueError, TypeError):
                pass

        query = TransactionListQuerySerializer(data=query_data)
        if not query.is_valid():
            return error_response(
                code="INVALID_TRANSACTION_EXPORT_QUERY",
                message="거래 내보내기 조건이 올바르지 않습니다.",
                errors=query.errors,
            )

        params = query.validated_data
        queryset = (
            Transaction.objects.filter(business=params["business"])
            .select_related("deduction_review")
            .order_by("-transaction_date", "-transaction_time", "-id")
        )

        if params.get("start_date"):
            queryset = queryset.filter(transaction_date__gte=params["start_date"])
        if params.get("end_date"):
            queryset = queryset.filter(transaction_date__lte=params["end_date"])
        if params.get("transaction_type"):
            queryset = queryset.filter(transaction_type=params["transaction_type"])
        else:
            # 지출 내역 분류 화면에서 내보내기 시 기본적으로 지출(매입) 내역을 내보낸다.
            queryset = queryset.filter(transaction_type=Transaction.TransactionType.PURCHASE)
        for field in ["source_type", "category", "expense_purpose"]:
            if params.get(field):
                queryset = queryset.filter(**{field: params[field]})

        output = io.StringIO()
        writer = csv.writer(output)

        # CSV 헤더 작성 (엑셀 친화적 한글 헤더)
        writer.writerow([
            "거래일자",
            "거래시간",
            "거래처명",
            "사업자등록번호",
            "거래구분",
            "수집출처",
            "카테고리",
            "지출구분",
            "부가세공제",
            "공급가액",
            "부가세액",
            "총금액",
            "승인번호",
        ])

        type_labels = {
            Transaction.TransactionType.PURCHASE: "매입(지출)",
            Transaction.TransactionType.SALE: "매출",
        }
        source_labels = {
            Transaction.SourceType.CARD_PURCHASE: "신용카드 매입",
            Transaction.SourceType.CASH_RECEIPT_SALE: "현금영수증 매출",
            Transaction.SourceType.TAX_INVOICE: "전자세금계산서",
        }
        purpose_labels = {
            Transaction.ExpensePurpose.BUSINESS: "사업 지출",
            Transaction.ExpensePurpose.PERSONAL: "개인 지출",
            Transaction.ExpensePurpose.UNCLASSIFIED: "미분류",
        }
        deduction_labels = {
            Transaction.SourceDeductionStatus.DEDUCTIBLE: "공제 대상",
            Transaction.SourceDeductionStatus.NON_DEDUCTIBLE: "불공제",
            Transaction.SourceDeductionStatus.UNKNOWN: "확인 필요",
        }

        for tx in queryset:
            # 부가세 공제 상태 결정 (개인 지출은 무조건 불공제)
            if tx.expense_purpose == Transaction.ExpensePurpose.PERSONAL:
                deduction_display = "불공제"
            elif hasattr(tx, "deduction_review") and tx.deduction_review and tx.deduction_review.confirmed_status:
                rev_status = str(tx.deduction_review.confirmed_status)
                if rev_status == "DEDUCTIBLE":
                    deduction_display = "공제 대상"
                elif rev_status == "NON_DEDUCTIBLE":
                    deduction_display = "불공제"
                else:
                    deduction_display = "확인 필요"
            else:
                deduction_display = deduction_labels.get(tx.source_deduction_status, "확인 필요")

            writer.writerow([
                tx.transaction_date.strftime("%Y-%m-%d") if tx.transaction_date else "",
                tx.transaction_time.strftime("%H:%M:%S") if tx.transaction_time else "",
                tx.merchant_name or "",
                tx.merchant_business_number or "",
                type_labels.get(tx.transaction_type, str(tx.transaction_type)),
                source_labels.get(tx.source_type, str(tx.source_type)),
                tx.get_category_display() if hasattr(tx, "get_category_display") else str(tx.category),
                purpose_labels.get(tx.expense_purpose, str(tx.expense_purpose)),
                deduction_display,
                int(tx.supply_amount) if tx.supply_amount is not None else 0,
                int(tx.vat_amount) if tx.vat_amount is not None else 0,
                int(tx.total_amount) if tx.total_amount is not None else 0,
                tx.approval_no or "",
            ])

        # UTF-8 with BOM for Excel compatibility
        csv_bytes = output.getvalue().encode("utf-8-sig")
        response = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8-sig")
        date_str = params.get("start_date", timezone.localdate()).strftime("%Y-%m") if params.get("start_date") else "all"
        response["Content-Disposition"] = f'attachment; filename="caffeine_transactions_{date_str}.csv"'
        return response


class TransactionDetailView(APIView):
    def get(self, request, transaction_id):
        business, error = _business_scope(request)
        if error:
            return error
        transaction = with_pending_duplicate_flag(
            Transaction.objects.filter(id=transaction_id, business=business)
        ).first()
        if transaction is None:
            return error_response(
                code="TRANSACTION_NOT_FOUND",
                message="거래를 찾을 수 없습니다.",
                status=404,
            )
        return success_response(
            code="TRANSACTION_DETAIL_SUCCESS",
            message="거래 상세 정보를 조회했습니다.",
            data=TransactionSerializer(transaction).data,
        )


class TransactionCategoryView(APIView):
    def patch(self, request, transaction_id):
        business, error = _business_scope(request)
        if error:
            return error
        transaction = Transaction.objects.filter(
            id=transaction_id,
            business=business,
        ).first()
        if transaction is None:
            return error_response(
                code="TRANSACTION_NOT_FOUND",
                message="거래를 찾을 수 없습니다.",
                status=404,
            )

        from tax.services.closing_service import MonthlyCloseService

        if MonthlyCloseService.is_closed(
            business_id=transaction.business_id,
            transaction_date=transaction.transaction_date,
        ):
            return error_response(
                code="MONTH_ALREADY_CLOSED",
                message="마감된 월의 거래는 수정할 수 없습니다.",
                status=409,
            )

        serializer = TransactionCategoryUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                code="INVALID_TRANSACTION_CATEGORY",
                message="카테고리 값이 올바르지 않습니다.",
                errors=serializer.errors,
            )

        transaction.category = serializer.validated_data["category"]
        transaction.classification_source = Transaction.ClassificationSource.USER
        transaction.classification_confidence = None

        from tax.models import DeductionReview
        from tax.services.deduction_service import DeductionReviewService

        if transaction.expense_purpose == Transaction.ExpensePurpose.BUSINESS:
            review = DeductionReviewService.get_or_create(transaction)
            if transaction.vat_amount == 0:
                is_deductible = transaction.category == Transaction.Category.RAW_MATERIAL
            else:
                is_deductible = True

            confirmed = (
                DeductionReview.ConfirmedStatus.DEDUCTIBLE
                if is_deductible
                else DeductionReview.ConfirmedStatus.NON_DEDUCTIBLE
            )
            DeductionReviewService.confirm(review=review, confirmed_status=confirmed)
            transaction.source_deduction_status = (
                Transaction.SourceDeductionStatus.DEDUCTIBLE
                if is_deductible
                else Transaction.SourceDeductionStatus.NON_DEDUCTIBLE
            )

        transaction.save(
            update_fields=[
                "category",
                "classification_source",
                "classification_confidence",
                "source_deduction_status",
                "updated_at",
            ]
        )
        transaction = with_pending_duplicate_flag(
            Transaction.objects.filter(pk=transaction.pk)
        ).get()
        return success_response(
            code="TRANSACTION_CATEGORY_UPDATED",
            message="거래 카테고리를 수정했습니다.",
            data=TransactionSerializer(transaction).data,
        )


class TransactionPurposeView(APIView):
    def patch(self, request, transaction_id):
        business, error = _business_scope(request)
        if error:
            return error
        transaction = Transaction.objects.filter(
            id=transaction_id,
            business=business,
        ).first()
        if transaction is None:
            return error_response(
                code="TRANSACTION_NOT_FOUND",
                message="거래를 찾을 수 없습니다.",
                status=404,
            )

        from tax.services.closing_service import MonthlyCloseService

        if MonthlyCloseService.is_closed(
            business_id=transaction.business_id,
            transaction_date=transaction.transaction_date,
        ):
            return error_response(
                code="MONTH_ALREADY_CLOSED",
                message="마감된 월의 거래는 수정할 수 없습니다.",
                status=409,
            )

        serializer = TransactionPurposeUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                code="INVALID_TRANSACTION_PURPOSE",
                message="지출 목적 값이 올바르지 않습니다.",
                errors=serializer.errors,
            )

        new_purpose = serializer.validated_data["expense_purpose"]
        transaction.expense_purpose = new_purpose
        transaction.expense_purpose_source = Transaction.ClassificationSource.USER

        from tax.models import DeductionReview
        from tax.services.deduction_service import DeductionReviewService

        if new_purpose == Transaction.ExpensePurpose.PERSONAL:
            review = DeductionReviewService.get_or_create(transaction)
            DeductionReviewService.confirm(
                review=review,
                confirmed_status=DeductionReview.ConfirmedStatus.NON_DEDUCTIBLE,
            )
            transaction.source_deduction_status = Transaction.SourceDeductionStatus.NON_DEDUCTIBLE
        elif new_purpose == Transaction.ExpensePurpose.BUSINESS:
            review = DeductionReviewService.get_or_create(transaction)
            if transaction.vat_amount == 0:
                is_deductible = transaction.category == Transaction.Category.RAW_MATERIAL
            else:
                is_deductible = True

            confirmed = (
                DeductionReview.ConfirmedStatus.DEDUCTIBLE
                if is_deductible
                else DeductionReview.ConfirmedStatus.NON_DEDUCTIBLE
            )
            DeductionReviewService.confirm(review=review, confirmed_status=confirmed)
            transaction.source_deduction_status = (
                Transaction.SourceDeductionStatus.DEDUCTIBLE
                if is_deductible
                else Transaction.SourceDeductionStatus.NON_DEDUCTIBLE
            )
        elif new_purpose == Transaction.ExpensePurpose.UNCLASSIFIED:
            review = DeductionReviewService.get_or_create(transaction)
            review.confirmed_status = DeductionReview.ConfirmedStatus.UNCONFIRMED
            review.confirmed_at = None
            review.save(update_fields=["confirmed_status", "confirmed_at", "updated_at"])
            transaction.source_deduction_status = Transaction.SourceDeductionStatus.UNKNOWN

        transaction.save(
            update_fields=[
                "expense_purpose",
                "expense_purpose_source",
                "source_deduction_status",
                "updated_at",
            ]
        )
        transaction = with_pending_duplicate_flag(
            Transaction.objects.filter(pk=transaction.pk)
        ).get()
        return success_response(
            code="TRANSACTION_PURPOSE_UPDATED",
            message="거래의 지출 목적을 수정했습니다.",
            data=TransactionSerializer(transaction).data,
        )


class TransactionDuplicateListView(APIView):
    def get(self, request):
        query = DuplicateListQuerySerializer(data=request.query_params)
        if not query.is_valid():
            return error_response(
                code="INVALID_DUPLICATE_QUERY",
                message="중복 거래 조회 조건이 올바르지 않습니다.",
                errors=query.errors,
            )

        params = query.validated_data
        queryset = TransactionDuplicate.objects.select_related(
            "primary_transaction",
            "primary_transaction__deduction_review",
            "suspected_transaction",
            "suspected_transaction__deduction_review",
        ).filter(business=params["business"], status=params["status"])
        data = _paginated_data(
            queryset,
            TransactionDuplicateSerializer,
            page=params["page"],
            page_size=params["page_size"],
            prepare_items=_mark_duplicate_relations,
        )
        return success_response(
            code="TRANSACTION_DUPLICATE_LIST_SUCCESS",
            message="중복 의심 거래 목록을 조회했습니다.",
            data=data,
        )


class TransactionDuplicateResolutionView(APIView):
    def patch(self, request, duplicate_id):
        business, error = _business_scope(request)
        if error:
            return error
        duplicate = TransactionDuplicate.objects.filter(
            id=duplicate_id,
            business=business,
        ).first()
        if duplicate is None:
            return error_response(
                code="TRANSACTION_DUPLICATE_NOT_FOUND",
                message="중복 거래 후보를 찾을 수 없습니다.",
                status=404,
            )

        serializer = DuplicateResolutionSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                code="INVALID_DUPLICATE_STATUS",
                message="중복 확정 상태가 올바르지 않습니다.",
                errors=serializer.errors,
            )

        duplicate.status = serializer.validated_data["status"]
        duplicate.resolved_at = timezone.now()
        duplicate.save(update_fields=["status", "resolved_at", "updated_at"])
        _mark_duplicate_relations([duplicate])
        return success_response(
            code="TRANSACTION_DUPLICATE_RESOLVED",
            message="중복 여부를 확정했습니다.",
            data=TransactionDuplicateSerializer(duplicate).data,
        )