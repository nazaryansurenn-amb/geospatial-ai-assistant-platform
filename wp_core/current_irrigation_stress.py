"""Deterministic possible-stress screen. All cutoffs are versioned local settings."""
import warnings
import numpy as np

INDICES=['ndvi','evi2','ndmi','vegetation','bare']

def median(x,axis=1):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',RuntimeWarning)
        return np.nanmedian(x,axis=axis)

def seasonal_features(dates,values,valid,times,year,cfg):
    """Use the latest clear observation, never an older greener replacement."""
    d=np.asarray(dates,dtype='datetime64[D]');n,t=valid.shape;ii=np.arange(n)
    early=(d>=np.datetime64(f"{year}-"+cfg['baseline']['start']))&(d<=np.datetime64(f"{year}-"+cfg['baseline']['end']))
    recent=(d>=np.datetime64(f"{year}-"+cfg['recent']['start']))&(d<=np.datetime64(f"{year}-"+cfg['recent']['end']))
    b=valid&early[None,:];r=valid&recent[None,:]
    latest=np.max(np.where(r,np.arange(t),-1),axis=1)
    safe=np.maximum(latest,0);age=(np.datetime64(f'{year}-09-05')-d[safe]).astype(int)
    prior=r&((d[safe,None]-d[None,:]).astype(int)>=cfg['recent']['minimum_separation_days'])
    previous=np.max(np.where(prior,np.arange(t),-1),axis=1);safe_p=np.maximum(previous,0)
    bmin=np.min(np.where(b,d.astype(int)[None,:],10**8),axis=1)
    bmax=np.max(np.where(b,d.astype(int)[None,:],-10**8),axis=1)
    covered=(b.sum(1)>=cfg['baseline']['minimum_dates'])&(bmax-bmin>=cfg['baseline']['minimum_span_days'])
    recent_ok=(latest>=0)&(previous>=0)&(age<=cfg['recent']['maximum_age_days'])
    covered &= recent_ok
    out={'covered':covered,'recent_covered':recent_ok,'baseline_count':b.sum(1),
         'last_day':np.where(latest>=0,d[safe].astype(int),-1),
         'previous_day':np.where(previous>=0,d[safe_p].astype(int),-1),
         'last_time':np.where(latest>=0,times[ii,safe],-1),
         'previous_time':np.where(previous>=0,times[ii,safe_p],-1)}
    for name in INDICES:
        x=values[name];out['base_'+name]=median(np.where(b,x,np.nan))
        out['last_'+name]=np.where(latest>=0,x[ii,safe],np.nan)
        out['previous_'+name]=np.where(previous>=0,x[ii,safe_p],np.nan)
    v=cfg['vegetation'];green=recent_ok.copy()
    for field in ['last','previous']:
        green &= (out[field+'_ndvi']>=v['ndvi'])&(out[field+'_evi2']>=v['evi2'])&(out[field+'_vegetation']>=v['fraction'])
    stable=covered&green&(out['base_ndvi']>=v['ndvi'])&(out['base_evi2']>=v['evi2'])&(out['base_vegetation']>=v['fraction'])
    for field in ['last','previous']:
        for name in ['ndvi','evi2']:stable &= out[field+'_'+name]>=out['base_'+name]*v['minimum_retention']
        stable &= out['base_vegetation']-out[field+'_vegetation']<=v['maximum_fraction_drop']
        stable &= out[field+'_bare']-out['base_bare']<=v['maximum_bare_increase']
    stable &= out['previous_ndvi']-out['last_ndvi']<=v['maximum_pair_ndvi_drop']
    stable &= out['previous_evi2']-out['last_evi2']<=v['maximum_pair_evi2_drop']
    out['current_vegetation']=green;out['stable_vegetation']=stable
    out['moisture_change']=(out['last_ndmi']+out['previous_ndmi'])/2-out['base_ndmi']
    out['repeated_moisture_drop']=(out['base_ndmi']-out['last_ndmi']>=cfg['signal']['repeated_ndmi_drop'])&(out['base_ndmi']-out['previous_ndmi']>=cfg['signal']['repeated_ndmi_drop'])
    return out

def classify(features,peer_change,peer_count,history_change,history_count,weather_complete,weather_support,cfg):
    f=features
    assessed=f['stable_vegetation']&(peer_count>=cfg['peers']['minimum'])&(history_count>=cfg['history']['minimum_comparable_years'])&weather_complete
    assessed &= np.isfinite(peer_change)&np.isfinite(history_change)
    peer_signal=f['moisture_change']-peer_change<=-cfg['signal']['peer_adjusted_drop']
    history_signal=f['moisture_change']-history_change<=-cfg['signal']['historical_adjusted_drop']
    candidate=assessed&f['repeated_moisture_drop']&peer_signal&history_signal&weather_support
    state=np.full(len(candidate),'unassessed',dtype=object)
    state[f['recent_covered']&~f['current_vegetation']]='not_current_vegetation'
    state[assessed]='not_flagged';state[candidate]='candidate'
    return {'assessed':assessed,'candidate':candidate,'state':state,'peer_signal':peer_signal,'historical_signal':history_signal}
