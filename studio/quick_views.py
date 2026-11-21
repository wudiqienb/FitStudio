import uuid
from django import forms
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import render, get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from .models import Member
from .services import BusinessError, credit_balance, _authorize
from .quick_services import quick_open, quick_renew, configured_course, reset_member_credentials
from .credential_services import authorize_reset


class PurchaseFields(forms.Form):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget,forms.HiddenInput):
                field.widget.attrs.update({'class':'form-control','style':'font-size:16px;min-height:46px'})

    credits = forms.IntegerField(label='购买课时数量', min_value=1, max_value=2147483647,
                                 widget=forms.NumberInput(attrs={'inputmode':'numeric','step':'1'}))
    amount = forms.DecimalField(label='实际收款金额（元）',min_value=0,max_digits=10,decimal_places=2,
                               widget=forms.NumberInput(attrs={'inputmode':'decimal','step':'.01'}))
    request_id = forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)


class OpenForm(PurchaseFields):
    name = forms.CharField(label='会员姓名',max_length=80)
    phone = forms.CharField(label='联系电话',max_length=64,widget=forms.TextInput(attrs={'type':'tel'}))
    field_order = ['name','phone','credits','amount','request_id']


class RenewForm(PurchaseFields):
    member = forms.IntegerField(widget=forms.HiddenInput,min_value=1)


@login_required
@never_cache
@require_http_methods(['GET','POST'])
@sensitive_post_parameters()
@sensitive_variables('password')
def quick_open_view(request):
    _authorize(request.user,'quick_open_member')
    form=OpenForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            order,password=quick_open(actor=request.user,**form.cleaned_data)
        except BusinessError as error: form.add_error(None,error)
        else:
            return render(request,'studio/quick_result.html',dict(order=order,password=password,
                username=order.member.login_account.user.username if password else None))
    return render(request,'studio/quick_form.html',dict(form=form,opening=True,presets=[5,10,20,30,50]))


@login_required
@never_cache
@require_http_methods(['GET','POST'])
@sensitive_post_parameters()
def quick_renew_view(request):
    _authorize(request.user,'quick_renew_member')
    query=request.GET.get('q','').strip()[:80]
    members=Member.objects.exclude(status=Member.Status.ARCHIVED).filter(Q(name__icontains=query)|Q(phone__icontains=query)).order_by('name','pk')[:20] if query else []
    form=RenewForm(request.POST if request.method=='POST' else None)
    member=None; balance=None; page_error=None
    raw=request.POST.get('member') if request.method=='POST' else request.GET.get('member')
    if raw:
        try: member=Member.objects.get(pk=int(raw))
        except (ValueError,OverflowError,Member.DoesNotExist): page_error='请选择已有会员。'
    if member:
        form.initial['member']=member.pk
        try: balance=credit_balance(member.pk,configured_course().pk)
        except BusinessError as error: page_error='；'.join(error.messages)
    if request.method=='POST' and form.is_valid():
        try:
            data=form.cleaned_data.copy(); member_id=data.pop('member')
            order=quick_renew(actor=request.user,member_id=member_id,**data)
        except BusinessError as error: form.add_error(None,error)
        else: return render(request,'studio/quick_result.html',dict(order=order))
    return render(request,'studio/quick_form.html',dict(form=form,member=member,balance=balance,members=members,query=query,page_error=page_error,presets=[5,10,20,30,50]))


@login_required
@never_cache
@require_http_methods(['GET','POST'])
@sensitive_post_parameters()
@sensitive_variables('password')
def reset_credentials_view(request,pk):
    actor=request.user
    authorize_reset(actor)
    member=get_object_or_404(Member,pk=pk)
    error=None
    if request.method=='POST' and request.POST.get('confirm')=='yes':
        try: binding,password=reset_member_credentials(actor=actor,member_id=pk)
        except BusinessError as exc: error='；'.join(exc.messages)
        else: return render(request,'studio/quick_result.html',dict(password=password,username=binding.user.username,reset=True))
    return render(request,'studio/quick_reset.html',dict(member=member,error=error))
