from django import forms

from .models import Member


class MemberForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._was_archived = self.instance.pk and self.instance.status == Member.Status.ARCHIVED
        if 'status' in self.fields:
            self.fields['status'].choices = [(value, label) for value, label in Member.Status.choices
                                             if value != Member.Status.ARCHIVED]

    def clean(self):
        data = super().clean()
        if self._was_archived:
            raise forms.ValidationError('已归档会员不可直接编辑，请先通过恢复流程恢复。')
        return data

    class Meta:
        model = Member
        fields = ("name", "phone", "status", "joined_on", "notes")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "autocomplete": "name"}),
            "phone": forms.TextInput(attrs={"class": "form-control", "type": "tel", "autocomplete": "tel"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "joined_on": forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class MemberDeleteForm(forms.Form):
    confirm = forms.BooleanField(label="我已核对档案，确认删除", widget=forms.CheckboxInput(
        attrs={"class": "form-check-input"}))


from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm


class MemberAccountOpeningForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs['class'] = 'form-control'
            field.widget.attrs['autocomplete'] = 'username' if name == 'username' else 'new-password'
        self.fields['username'].label = '会员登录账号'
        self.fields['username'].widget.attrs.update(autocapitalize='none', spellcheck='false')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = False
        user.is_superuser = False
        user.is_active = True
        if commit:
            user.save()
        return user


class MemberCreateForm(MemberForm):
    open_account = forms.BooleanField(required=False, label='同时开通会员账号',
                                     widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    username = forms.CharField(required=False, label='会员登录账号', widget=forms.TextInput(attrs={'class':'form-control','autocomplete':'username','autocapitalize':'none'}))
    password1 = forms.CharField(required=False, label='会员登录密码', widget=forms.PasswordInput(attrs={'class':'form-control','autocomplete':'new-password'}))
    password2 = forms.CharField(required=False, label='再次输入密码', widget=forms.PasswordInput(attrs={'class':'form-control','autocomplete':'new-password'}))

    def __init__(self, *args, allow_account=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_account:
            for name in ('open_account', 'username', 'password1', 'password2'):
                self.fields.pop(name)

    def clean(self):
        data = super().clean()
        self.account_form = None
        if data.get('open_account'):
            self.account_form = MemberAccountOpeningForm(data={k:self.data.get(k,'') for k in ('username','password1','password2')})
            if not self.account_form.is_valid():
                for name, errors in self.account_form.errors.items():
                    self.add_error(name if name in self.fields else None, errors)
        return data
