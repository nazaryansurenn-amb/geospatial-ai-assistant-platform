"""Array-based implementation of the existing matched-parcel rules.

Thresholds and interpretation are unchanged. Acquisitions, weather cells,
platforms and years remain separate. Tests compare against the frozen reference.
"""
import numpy as np
import pandas as pd
from wp_core import sentinel1_peer_comparison as reference


def potential_peers(fields, cfg):
    rows=[]
    for cell,g in fields.groupby("cell_id",sort=True):
        g=g.loc[g.sample_group.eq("clean_interior")]
        ids=g.internal_parcel_id.to_numpy(); x=g.x_m.to_numpy(); y=g.y_m.to_numpy(); areas=g.area_official_m2.to_numpy()
        for i,pid in enumerate(ids):
            d=np.hypot(x-x[i],y-y[i]); ratio=np.maximum(areas,areas[i])/np.minimum(areas,areas[i])
            keep=(ids!=pid)&(d<=cfg["maximum_neighbor_distance_m"])&(ratio<=cfg["maximum_area_ratio"])
            rows.extend((str(pid),str(ids[j]),float(d[j]),float(ratio[j])) for j in np.flatnonzero(keep))
    return pd.DataFrame(rows,columns=["internal_parcel_id","peer_id","distance_m","area_ratio"])


def join_optical(observations,eo):
    left=observations.copy()
    left["radar_time"]=pd.to_datetime(left.datetime,utc=True,format="ISO8601")
    right=eo.loc[eo.support.ge(.6)&eo.finite.astype(bool)].copy()
    right["optical_time"]=pd.to_datetime(right.observation_date,utc=True)
    right["eo_date"]=right.observation_date.astype(str)
    right=right.rename(columns={n+"_median":"eo_"+n for n in ("ndvi","bsi","ndmi")})
    right=right[["internal_parcel_id","optical_time","eo_date","eo_ndvi","eo_ndmi","eo_bsi"]]
    assert not right.duplicated(["internal_parcel_id","optical_time"]).any()
    merged=pd.merge_asof(left.sort_values("radar_time"),right.sort_values("optical_time"),
                         left_on="radar_time",right_on="optical_time",by="internal_parcel_id",direction="nearest",tolerance=pd.Timedelta(days=3))
    return merged.drop(columns=["radar_time","optical_time"]).sort_values(["internal_parcel_id","datetime","relative_orbit"]).reset_index(drop=True)


def compare(observations,fields,potential,rain,cfg):
    fields=fields.sort_values("internal_parcel_id").reset_index(drop=True)
    ids=fields.internal_parcel_id.to_numpy(); n=len(ids)
    index={pid:i for i,pid in enumerate(ids)}
    neighbors={}
    for pid,g in potential.groupby("internal_parcel_id",sort=False):
        g=g.loc[g.peer_id.isin(index)]
        neighbors[index[pid]]=(np.array([index[p] for p in g.peer_id],dtype=int),g.distance_m.to_numpy())
    records=[]; peer_links=[]; metric_rows=[]; signals=[]
    rain_limits=cfg["rain_sensitivity_mm"]; db_limits=cfg["excursion_sensitivity_db"]
    for (orbit,platform),frame in observations.groupby(["relative_orbit","platform"],sort=True):
        acquisitions=frame[["source_item","datetime"]].drop_duplicates().sort_values("datetime")
        sources=acquisitions.source_item.tolist(); times=pd.to_datetime(acquisitions.datetime,utc=True,format="ISO8601").tolist()
        assert len(sources)==len(set(sources))
        if len(sources)<3:
            continue
        ordered=frame.set_index(["source_item","internal_parcel_id"]).reindex(pd.MultiIndex.from_product([sources,ids]))
        assert len(ordered)==len(sources)*n
        def panel(column):
            return ordered[column].to_numpy().reshape(len(sources),n)
        vv=panel("vv_inner_median_db").astype(float); vh=panel("vh_inner_median_db").astype(float)
        ndvi=panel("eo_ndvi").astype(float); bsi=panel("eo_bsi").astype(float); ndmi=panel("eo_ndmi").astype(float)
        usable=ordered.radar_usable.fillna(False).to_numpy(dtype=bool).reshape(len(sources),n)
        dates=pd.to_datetime(ordered.eo_date,utc=True).astype("int64").to_numpy().reshape(len(sources),n)
        date_known=ordered.eo_date.notna().to_numpy().reshape(len(sources),n)
        count=np.zeros((n,len(rain_limits),len(db_limits)),dtype=int)
        own=count.copy(); relative=count.copy(); peer_sum=np.zeros(count.shape,dtype=float)
        for k in range(1,len(sources)-1):
            before_days=(times[k]-times[k-1]).total_seconds()/86400; after_days=(times[k+1]-times[k]).total_seconds()/86400
            a=ndvi[k-1:k+2]; b=bsi[k-1:k+2]
            supported=(usable[k-1:k+2].all(axis=0)&date_known[k-1:k+2].all(axis=0)&np.isfinite(a).all(axis=0)&np.isfinite(b).all(axis=0)
                       &(np.ptp(a,axis=0)<=cfg["maximum_ndvi_range"])&(np.ptp(b,axis=0)<=cfg["maximum_bsi_range"])
                       &(ndvi[k]>=cfg["minimum_middle_ndvi"])&(ndvi[k]<=cfg["maximum_middle_ndvi"])
                       &(0<before_days<=cfg["maximum_track_gap_days"])&(0<after_days<=cfg["maximum_track_gap_days"]))
            rise=vv[k]-vv[k-1]; fall=vv[k]-vv[k+1]
            peers_count=np.zeros(n,dtype=int); med_rise=np.full(n,np.nan); med_fall=np.full(n,np.nan); fractions=np.full((n,len(db_limits)),np.nan)
            for i in np.flatnonzero(supported):
                candidate,dist=neighbors.get(i,(np.array([],dtype=int),np.array([],dtype=float)))
                keep=supported[candidate].copy(); score=np.zeros(len(candidate))
                for j in range(3):
                    for matrix,limit in ((a,cfg["maximum_ndvi_difference"]),(b,cfg["maximum_bsi_difference"])):
                        delta=np.abs(matrix[j,candidate]-matrix[j,i]); keep&=delta<=limit; score+=delta/limit
                    keep&=np.abs(dates[k-1+j,candidate]-dates[k-1+j,i])/86400e9<=cfg["maximum_optical_date_difference_days"]
                candidate=candidate[keep]; dist=dist[keep]; score=score[keep]
                order=np.lexsort((ids[candidate],dist,score))[:cfg["maximum_peers"]]
                candidate=candidate[order]; dist=dist[order]; score=score[order]
                if len(candidate):
                    peers_count[i]=len(candidate); med_rise[i]=np.median(rise[candidate]); med_fall[i]=np.median(fall[candidate])
                    for q,db in enumerate(db_limits): fractions[i,q]=((rise[candidate]>=db)&(fall[candidate]>=db)).mean()
                    peer_links.extend({"internal_parcel_id":ids[i],"peer_id":ids[j],"first_item":sources[k-1],"middle_item":sources[k],"last_item":sources[k+1],
                                       "match_score":float(score[pos]),"distance_m":float(dist[pos])} for pos,j in enumerate(candidate))
            per_cell={}
            for cell in fields.cell_id.unique():
                series=rain[cell]
                per_cell[cell]=[reference.rain_between(series,times[k-1],times[k]),reference.rain_between(series,times[k],times[k+1]),
                                reference.rain_between(series,times[k]-pd.Timedelta(hours=48),times[k])]
            rain_values=np.array([per_cell[cell] for cell in fields.cell_id])
            known=np.isfinite(rain_values).all(axis=1)
            enough=supported&(peers_count>=cfg["minimum_peers"])
            for i,pid in enumerate(ids):
                records.append({"internal_parcel_id":pid,"relative_orbit":int(orbit),"platform":platform,"first_item":sources[k-1],"middle_item":sources[k],"last_item":sources[k+1],
                                "first_datetime":times[k-1].isoformat(),"middle_datetime":times[k].isoformat(),"last_datetime":times[k+1].isoformat(),
                                "supported":bool(supported[i]),"peer_count":int(peers_count[i]),"weather_complete":bool(known[i]),"cell_id":fields.cell_id.iloc[i],
                                "rain_before_mm":rain_values[i,0],"rain_after_mm":rain_values[i,1],"rain_recent_48h_mm":rain_values[i,2],
                                "rise_db":rise[i],"fall_db":fall[i],"fall_db_per_day":fall[i]/after_days,
                                "vh_rise_db":vh[k,i]-vh[k-1,i],"vh_fall_db":vh[k,i]-vh[k+1,i],"ndmi_change_after":ndmi[k+1,i]-ndmi[k,i],
                                "peer_median_rise_db":med_rise[i],"peer_median_fall_db":med_fall[i]})
            for r,limit in enumerate(rain_limits):
                comparable=enough&known&(np.max(rain_values,axis=1)<=limit)
                for d,db in enumerate(db_limits):
                    event=(rise>=db)&(fall>=db)
                    rel=event&(rise-med_rise>=cfg["minimum_peer_contrast_db"])&(fall-med_fall>=cfg["minimum_peer_contrast_db"])
                    count[:,r,d]+=comparable
                    own[:,r,d]+=comparable&event
                    relative[:,r,d]+=comparable&rel
                    peer_sum[comparable,r,d]+=fractions[comparable,d]
                    signals.extend({"internal_parcel_id":ids[i],"relative_orbit":int(orbit),"platform":platform,"first_item":sources[k-1],"middle_item":sources[k],"last_item":sources[k+1],
                                    "middle_datetime":times[k].isoformat(),"rain_limit_mm":float(limit),"excursion_db":float(db),
                                    "relative_excursion":bool(rel[i]),"observed_excursion":True} for i in np.flatnonzero(comparable&event))
        for i,pid in enumerate(ids):
            for r,limit in enumerate(rain_limits):
                for d,db in enumerate(db_limits):
                    c=int(count[i,r,d]); own_rate=own[i,r,d]/c if c else np.nan; peer_rate=peer_sum[i,r,d]/c if c else np.nan
                    sufficient=c>=cfg["minimum_comparable_triplets"]
                    metric_rows.append({"internal_parcel_id":pid,"relative_orbit":int(orbit),"platform":platform,"rain_limit_mm":float(limit),"excursion_db":float(db),
                                        "all_triplets":len(sources)-2,"comparable_triplets":c,"observed_excursions":int(own[i,r,d]) if c else None,
                                        "relative_excursions":int(relative[i,r,d]) if c else None,"observed_excursion_fraction":own_rate,"matched_peer_fraction":peer_rate,
                                        "excess_fraction":own_rate-peer_rate,"repeated_relative_signal":bool(relative[i,r,d]>=cfg["minimum_relative_excursions"] and own_rate-peer_rate>=cfg["minimum_excess_rate"]) if sufficient else None})
    metrics=pd.DataFrame(metric_rows)
    if len(metrics): metrics["repeated_relative_signal"]=pd.array(metrics.repeated_relative_signal,dtype="boolean")
    return pd.DataFrame(records),pd.DataFrame(peer_links,columns=["internal_parcel_id","peer_id","middle_item","first_item","last_item","match_score","distance_m"]),metrics,pd.DataFrame(signals,columns=["internal_parcel_id","relative_orbit","platform","first_item","middle_item","last_item","middle_datetime","rain_limit_mm","excursion_db","relative_excursion","observed_excursion"])
