from __future__ import print_function
import six
import numpy as np
import glob
import os
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
import datetime
from osgeo import osr
from stompy import utils,filters
from stompy.spatial import wkb2shp, proj_utils

def bin_mean(bins,values):
    sums=np.bincount(bins,weights=values)
    counts=np.bincount(bins)
    return sums/counts


def mark_gaps(dnums,valid,gap_days,yearday_start=-1,yearday_end=367,include_ends=False):
    """
    for a timeseries, assumed to be dense and daily,
    return a mask which is true for gaps in valid data
    which span at least gap_days, limited to the portion of 
    the year given by yearday_start,yearday_end.
    include_ends: include the possibility of a gap of gap_days//2 at the beginning
    and end of the series (i.e. as if the next valid data point were very far off
    the end of the series)
    """
    doy=np.array([d - utils.dnum_jday0(d)
                  for d in dnums] )

    missing=~valid
    in_window=(doy>=yearday_start)&(doy<yearday_end)
    present=np.nonzero( ~missing | ~in_window)[0]

    mask=np.zeros( len(dnums),np.bool )

    for gstart,gend in zip( present[:-1],present[1:] ):
        if gend-gstart<gap_days:
            continue
        mask[ gstart+gap_days//2 : gend-gap_days//2 ] = True
        
    if include_ends:
        # too tired to think through the logic of how the ends combined with
        # the yeardays.
        assert yearday_start<0
        assert yearday_end>366
        first_gap=max(0,present[0]-gap_days//2)
        mask[:first_gap]=True
        final_gap=min( len(mask), present[-1]+gap_days//2 )
        mask[final_gap:]=True
    return mask

compile_dir="../outputs/intermediate/delta"
fig_dir="../outputs/figures"
output_dir="../outputs"

date_start=datetime.datetime(2000,1,1)
date_end  =datetime.datetime(2016,12,31)
dn_start=utils.to_dnum(date_start)
dn_end  =utils.to_dnum(date_end)
dns=np.arange(dn_start,dn_end+1)
fmt='%Y-%m-%d'

ds=xr.Dataset()
ds['time']=utils.to_dt64(dns)
ds['dnum']=('time',dns)
ds=ds.set_coords('dnum')

analytes=['flow',
          'NO3_conc', 'NO4_conc', 'NO2_conc', 'NN_conc', 'NH3_conc', 'NH4_conc', 'PO4_conc']
		  
# These match the names of the CSV files
site_names=['Davis', 'Manteca', 'Tracy', 'Stockton', 'RegionalSan', 'Sacramento', 'SanJoaquin']
ds['site']=( 'site', site_names)

			
# initialize full output array
for analyte in analytes:
    ds[analyte]=( ['time','site'],
             np.nan*np.ones( (len(ds.time),len(ds.site)) ) )

# set units for clarity upfront
ds.flow.attrs['units']='m3 s-1'
ds.NH3_conc.attrs['units']='mg/l N'
ds.NH4_conc.attrs['units']='mg/l N'
ds.NO3_conc.attrs['units']='mg/l N'
ds.NO2_conc.attrs['units']='mg/l N'
ds.NN_conc.attrs['units']='mg/l N'
ds.PO4_conc.attrs['units']='mg/l P'

# setup flag entries
for v in ds.data_vars.keys():
    ds[v+'_flag']=( ds[v].dims, np.zeros(ds[v].shape,'i2'))
    ds[v].attrs['flags']=v+'_flag'
		 

FLAG_SEASONAL_TREND=1
FLAG_INTERP=2
FLAG_MEAN=4
FLAG_CLIPPED=8 # this one actually does get used as a bitmask.
flag_bits=['Trend','Interp','Mean','Clipped']		 


# Read in Loading Study data via one csv per site
for site in ds.site: 
    site=site.item() # get to a str object
    # site_idx=list(ds.site).index(site) # 11

    csv=pd.read_csv(os.path.join(compile_dir,site+'.csv'),
                        parse_dates=['Date'])
    csv_dnums=utils.to_dnum(csv.Date)
    csv_date_i = np.searchsorted(dns,csv_dnums)
 
    # limit to the overlap between csv dates and output dates
    date_valid=(csv_dnums>=dns[0]) & (csv_dnums<dns[-1])
	
    # FLOW
    if 'flow mgd' in csv:
	      flow=0.043812636*csv['flow mgd'] # convert to cubic meters / second
	      valid=date_valid & (~flow.isnull().values)
	      ds['flow'].sel(site=site)[csv_date_i[valid]] = flow[valid]
	      flow_valid=valid 
	      
    if 'NO3 mg/L N' in csv:
        no3=csv['NO3 mg/L N']
        valid=date_valid & (~no3.isnull().values)
        ds['NO3_conc'].sel(site=site)[csv_date_i[valid]] = no3[valid]
        no3_valid=valid

    if 'NO2 mg/L N' in csv:
        no2=csv['NO2 mg/L N']
        valid=date_valid & (~no2.isnull().values)
        ds['NO2_conc'].sel(site=site)[csv_date_i[valid]] = no2[valid]
        no2_valid=valid
        
    if 'N+N mg/L N' in csv:
        nn=csv['N+N mg/L N']
        valid=date_valid & (~nn.isnull().values)
        ds['NN_conc'].sel(site=site)[csv_date_i[valid]] = nn[valid]
        nn_valid=valid

    if 'NH3 mg/L N' in csv:
        nh3=csv['NH3 mg/L N']
        valid=date_valid & (~nh3.isnull().values)
        ds['NH3_conc'].sel(site=site)[csv_date_i[valid]] = nh3[valid]
        nh3_valid=valid
        
    if 'PO4 mg/L P' in csv:
        po4=csv['PO4 mg/L P']
        valid=date_valid & (~po4.isnull().values)
        ds['PO4_conc'].sel(site=site)[csv_date_i[valid]] = po4[valid]
        po4_valid=valid      

# The interpolation step - building off of synth_v02.py

fields=[s for s in ds.data_vars if not s.endswith('_flag')]

lowpass_days=3*365
shortgap_days=45 # okay to interpolate a little over a month?

# first, create mapping from time index to absolute month
dts=utils.to_datetime(dns)
absmonth = [12*dt.year + (dt.month-1) for dt in dts]
absmonth = np.array(absmonth) - dts[0].year*12
month=absmonth%12

for site in ds.site.values:
    print("Site: %s"%site)
    for fld in fields: 
        fld_in=ds[fld].sel(site=site)
        orig_values=fld_in.values
        fld_flag=ds[fld+'_flag'].sel(site=site)

        prefilled=fld_flag.values & (FLAG_SEASONAL_TREND | FLAG_INTERP | FLAG_MEAN)        
        fld_in.values[prefilled]=np.nan # resets the work of this loop in case it's run multiple times
        n_valid=np.sum( ~fld_in.isnull())        
        
        if n_valid==0:
            msg=" --SKIPPING--"
        else:
            msg=""
        print("   field: %s  %d/%d valid input points %s"%(fld,n_valid,len(fld_in),msg))

        if n_valid==0:
            continue
            
        # get the data into a monthly time series before trying to fit seasonal cycle
        valid = np.isfinite(fld_in.values)
        absmonth_mean=bin_mean(absmonth[valid],fld_in.values[valid])
        month_mean=bin_mean(month[valid],fld_in.values[valid])
        
        if np.sum(np.isfinite(month_mean)) < 12:
            print("Insufficient data for seasonal trends - will fill with sample mean")
            trend_and_season=np.nanmean(month_mean) * np.ones(len(dns))
            t_and_s_flag=FLAG_MEAN
        else:
            # fit long-term trend and a stationary seasonal cycle
            # this removes both the seasonal cycle and the long-term mean,
            # leaving just the trend
            trend_hf=fld_in.values - month_mean[month]
            lp = filters.lowpass_fir(trend_hf,lowpass_days,nan_weight_threshold=0.01)
            trend = utils.fill_invalid(lp)
            # recombine with the long-term mean and monthly trend 
            # to get the fill values.
            trend_and_season = trend + month_mean[month]
            t_and_s_flag=FLAG_SEASONAL_TREND

        # long gaps are mostly filled by trend and season
        gaps=mark_gaps(dns,valid,shortgap_days,include_ends=True) 
        fld_in.values[gaps] = trend_and_season[gaps]
        fld_flag.values[gaps] = t_and_s_flag

        still_missing=np.isnan(fld_in.values)
        fld_in.values[still_missing] = utils.fill_invalid(fld_in.values)[still_missing]
        fld_flag.values[still_missing] = FLAG_INTERP

        # Make sure all flows are nonnegative
        negative=fld_in.values<0.0
        fld_in.values[negative]=0.0
        fld_flag.values[negative] |= FLAG_CLIPPED
        if 0: # illustrative plots
            fig,ax=plt.subplots()
            ax.plot(dns,orig_values,'m-o',label='Measured %s'%fld)
            ax.plot(dns,fld_in,'k-',label='Final %s'%fld,zorder=5)
            # ax.plot(dns,month_mean[month],'r-',label='Monthly Clim.')
            # ax.plot(dns,trend_hf,'b-',label='Trend w/HF')
            ax.plot(dns,trend,'g-',lw=3,label='Trend')
            ax.plot(dns,trend_and_season,color='orange',label='Trend and season')
            
# keep timebase consistent between files
nc_path=os.path.join(output_dir,'delta_potw.nc')
os.path.exists(nc_path) and os.unlink(nc_path)
encoding={'time':dict(units="seconds since 1970-01-01 00:00:00")}
ds.to_netcdf(nc_path,encoding=encoding)

# And write an xls file, too.  Reload from disk to ensure consitency.
ds=xr.open_dataset(os.path.join(output_dir,'delta_potw.nc'))

writer = pd.ExcelWriter( os.path.join(output_dir,'delta_potw.xlsx'))

# Break that out into one sheet per source
for site_name in ds.site.values:
    print(site_name)
    df=ds.sel(site=site_name).to_dataframe()

    df.to_excel(writer,site_name)
writer.save()