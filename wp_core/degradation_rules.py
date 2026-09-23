"""Independent five-year vegetation screening; missing observations stay unknown."""
from calendar import monthrange
from datetime import date
import warnings
import numpy as np

def nanmedian(a,axis):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',RuntimeWarning)
        return np.nanmedian(a,axis=axis)

def annual_features(dates,ndvi,evi2,valid,year,policy):
    """Nine calendar-month medians, weighted by days; no gap interpolation.

    These are seasonal vegetation proxies, not measured NPP, crop yield or the
    formal 16-year UNCCD productivity series.
    """
    dates=np.asarray(dates,dtype='datetime64[D]');valid=valid.copy()
    assert ndvi.shape==evi2.shape==valid.shape and ndvi.shape[1]==len(dates)
    assert np.all(np.diff(dates)>np.timedelta64(0,'D'))
    days=(dates-np.datetime64(f'{year}-01-01')).astype(int)+1
    for j in range(1,len(dates)-1):
        adjacent=valid[:,j-1:j+2].all(axis=1) & (days[j+1]-days[j-1]<=30)
        isolated=adjacent & (np.abs(ndvi[:,j-1]-ndvi[:,j+1])<.10) & (np.abs(evi2[:,j-1]-evi2[:,j+1])<.08)
        isolated &= (np.abs(ndvi[:,j]-ndvi[:,j-1])>.35)&(np.abs(ndvi[:,j]-ndvi[:,j+1])>.35)
        isolated &= (np.abs(evi2[:,j]-evi2[:,j-1])>.25)&(np.abs(evi2[:,j]-evi2[:,j+1])>.25)
        valid[isolated,j]=False
    counts=[];nvalues=[];evalues=[]
    for month in range(3,12):
        start=np.datetime64(date(year,month,1));end=np.datetime64(date(year,month,monthrange(year,month)[1]))
        selected=(dates>=start)&(dates<=end);good=valid[:,selected]
        counts.append(good.sum(axis=1))
        nvalues.append(nanmedian(np.where(good,ndvi[:,selected],np.nan),1))
        evalues.append(nanmedian(np.where(good,evi2[:,selected],np.nan),1))
    counts=np.array(counts).T;nvalues=np.array(nvalues).T;evalues=np.array(evalues).T
    start=(date(year,3,1)-date(year,1,1)).days+1;end=(date(year,11,30)-date(year,1,1)).days+1
    previous=np.full(len(valid),start);gaps=np.zeros(len(valid),int)
    for j,day in enumerate(days):
        if start<=day<=end:
            good=valid[:,j];gaps=np.maximum(gaps,np.where(good,day-previous,0));previous=np.where(good,day,previous)
    gaps=np.maximum(gaps,end-previous)
    covered=(counts>=policy['minimum_dates_per_month']).all(axis=1)&(gaps<=policy['maximum_gap_days'])
    weights=np.array([monthrange(year,m)[1] for m in range(3,12)])
    n=np.sum(nvalues*weights,axis=1)/weights.sum();e=np.sum(evalues*weights,axis=1)/weights.sum()
    return {'covered':covered,'ndvi':np.where(covered,n,np.nan),'evi2':np.where(covered,e,np.nan),
            'usable_dates':counts.sum(axis=1),'maximum_gap_days':gaps,'monthly_dates':counts,'monthly_ndvi':nvalues,'monthly_evi2':evalues}

def sen_slope(values):
    return nanmedian(np.stack([(values[:,j]-values[:,i])/(j-i) for i in range(5) for j in range(i+1,5)],axis=1),1)

def flags(ndvi,evi2,peer_median_ndvi,peer_median_evi2,peer_p90_ndvi,peer_p90_evi2,config):
    """Return separate indicator eligibility and flags; never count missing as low."""
    n,e=np.asarray(ndvi),np.asarray(evi2);d=config['deterioration'];p=config['low_performance']
    measured=np.isfinite(n)&np.isfinite(e)
    normalized_n=np.divide(n,peer_median_ndvi,out=np.full_like(n,np.nan),where=peer_median_ndvi>.10)
    normalized_e=np.divide(e,peer_median_evi2,out=np.full_like(e,np.nan),where=peer_median_evi2>.05)
    assessed=measured.all(axis=1)&np.isfinite(normalized_n).all(axis=1)&np.isfinite(normalized_e).all(axis=1)
    early_n=nanmedian(n[:,:2],1);early_e=nanmedian(e[:,:2],1)
    recent_n=nanmedian(n[:,3:],1);recent_e=nanmedian(e[:,3:],1)
    deterioration=assessed & (early_n>=d['minimum_baseline_ndvi']) & (early_e>=d['minimum_baseline_evi2'])
    deterioration &= (early_n-recent_n>=d['ndvi_absolute_drop'])&(early_e-recent_e>=d['evi2_absolute_drop'])
    deterioration &= (recent_n<=early_n*(1-d['relative_drop']))&(recent_e<=early_e*(1-d['relative_drop']))
    deterioration &= (n[:,3:]<early_n[:,None]).all(axis=1)&(e[:,3:]<early_e[:,None]).all(axis=1)
    deterioration &= (sen_slope(n)<=d['ndvi_slope'])&(sen_slope(e)<=d['evi2_slope'])
    deterioration &= (nanmedian(normalized_n[:,:2],1)-nanmedian(normalized_n[:,3:],1)>=d['peer_adjusted_drop'])
    deterioration &= (nanmedian(normalized_e[:,:2],1)-nanmedian(normalized_e[:,3:],1)>=d['peer_adjusted_drop'])
    comparable=measured & np.isfinite(peer_p90_ndvi)&np.isfinite(peer_p90_evi2)
    comparable &= (peer_p90_ndvi>=p['minimum_reference_ndvi'])&(peer_p90_evi2>=p['minimum_reference_evi2'])
    low_year=comparable&(n<peer_p90_ndvi*p['ratio'])&(e<peer_p90_evi2*p['ratio'])
    low_assessed=(comparable.sum(axis=1)>=p['minimum_years'])&comparable[:,3:].all(axis=1)
    low=low_assessed&(low_year.sum(axis=1)>=p['minimum_years'])&low_year[:,3:].all(axis=1)
    return {'deterioration_assessed':assessed,'deterioration':deterioration,'low_performance_assessed':low_assessed,
            'low_performance':low,'low_years':low_year.sum(axis=1),'covered_years':measured.sum(axis=1),
            'ndvi_drop':early_n-recent_n,'evi2_drop':early_e-recent_e,'ndvi_slope':sen_slope(n),'evi2_slope':sen_slope(e)}
