from __future__ import print_function
import pickle
import time
import subprocess
import os
import piexif
import pytz
import concurrent.futures
import asyncio
import aiohttp
from tqdm.asyncio import tqdm_asyncio
from PIL import Image
from typing import Iterator
from datetime import datetime
from tqdm import tqdm
from dotenv import load_dotenv 
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

#TO DO:
#-Fix issues with tqdm and async/multithreading. Bar seems to just jump.
#-Clean up code (e.g. rename variables to better reflect content).

#NOTES:
#-It may be a good idea to use an async google api library like aiogoogle. Currently the service object
# takes a significant amount of time to get a response.
#
#-The multithreaded code for grabbing album media could be possibly rewritten using async instead. Though
# this may not be the effort.
#
#-Currently the list of photos not apart of an album and list of photos possessing no EXIF data are global
# variables. It would be better to just pass them in as arguments to the functions but I don't see any
# benefit in doing that as of the moment.

load_dotenv()
list_no_exif = [] 
list_no_album = []
FILE_DIR = os.getenv('PHOTO_DIRECTORY')
TIME_NAME_FORMAT = "%Y-%m-%d %H.%M.%S"

def utc_to_pt(utc_string:'str',format:'str'="%Y:%m:%d %H:%M:%S",return_obj:'bool'=False) -> str | datetime:
    """
    Converts UTC date to PT date accounting for daylight savings
  
    Converts a string in a specific UTC date format to a PT date in a given format. 
    Accounts for daylight savings (PST and PDT).
  
    Parameters
    ----------
    utc_string (string): UTC date whose format is %Y-%m-%dT%H:%M:%SZ.
    format (string), optional: The string date format of the returned PT date.
    return_obj (boolean), optional: If true then it will return the PT date as a datetime object. Else it will
                                    return it as a string.
  
    Returns
    ----------
    pt_date (string/datetime.datetime): A date in the pacific timezone as a string or a datetime object.
    """

    utc_timezone = pytz.timezone("UTC")
    date = utc_timezone.localize(datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ"))

    pt_timezone = pytz.timezone("US/Pacific")

    #Convert UTC to PT
    pt_date = date.astimezone(pt_timezone)

    if(return_obj):
        return pt_date
    else:
        return pt_date.strftime(format)

#Normally dates would work using the filters param but albumId can't be set in conjunction with any filters.
#NOTE: Photos in each album must either be correctly sorted as newest first or oldest first. 
# The code might still work without it but there are no guarantees.
def get_media(service, params:dict, start_date:datetime = datetime(2022,8,24)) -> Iterator[dict]: 
    """
    Generator for getting all photos from an album
  
    Generator function which uses the nextPageToken to get all photos from a given album starting from a given date.
  
    Parameters
    ----------
    service : The resource API object.
    params (dict): Search parameters.
    start_date (datetime), optional: The starting date from which photos will be obtained.
  
    Returns
    ----------
    An iterator of the dictionary type.
    """

    photo_date = None

    start_date = pytz.UTC.localize(start_date)

    while True:

        request = service.mediaItems().search(**params)
        response = request.execute()

        for content in response['mediaItems']:
            photo_date = utc_to_pt(content['mediaMetadata']['creationTime'],return_obj=True)
            if(photo_date < start_date):
                return
            yield content
            
        params['pageToken'] = response.get('nextPageToken')

        if (response.get('nextPageToken') is None):
            break

def get_album_thread(creds,album:dict, start_date:datetime = datetime(2022,8,24))-> list[dict]:
    """
    Thread function for getting album content
  
    Function to be used in ThreadPoolExecutor to get album content.
  
    Parameters
    ----------
    creds : Credentials object used by Google for building a service object.
    album (dict): A dictionary containing information about the album. Most important key:value is 'id'.
    start_date (datetime), optional: The starting date from which album content will be obtained.
  
    Returns
    ----------
    media_content (list): A list of dictionaries representing each photo in the album
    """

    #The service object is naturally not thread-safe so we make one for each worker.
    service = build('photoslibrary', 'v1', credentials=creds, static_discovery=False)

    search_params = {
        "albumId": {
            "description": "Identifier of an album. If populated, lists all media items in specified album. Can't set in conjunction with any filters.",
            "location": "query",
            "type": "string"
        },
        "pageSize": {
            "description": "Maximum number of media items to return in the response. Fewer media items might be returned than the specified number. The default pageSize is 25, the maximum is 100.",
            "location": "query",
            "type": "integer",
            "format": "int32"
        },
        "pageToken": {
            "description": "A continuation token to get the next page of the results. Adding this to the request returns the rows after the pageToken. The pageToken should be the value returned in the nextPageToken parameter in the response to the searchMediaItems request.",
            "location": "query",
            "type": "string"
        },
        "filters": {
            "description": "Filters to apply to the request. Can't be set in conjunction with an albumId.",
            "location": "query",
            "type": "object"
        }
    }

    service._resourceDesc['resources']['mediaItems']['methods']['search']['parameters'].update(search_params)

    album_id = album['id']
    media_content = [content for content in get_media(service,{'albumId':album['id'], 'pageSize':20},start_date=start_date)]
    media_content.insert(0,album_id) #Multithreading does not guarantee order so we use the album id for reordering later.
    return media_content


async def save_media(photo:dict, session:aiohttp.ClientSession) -> None:
    """
    Function for saving a single photo/video from API to folder.
  
    Async function that downloads the photo/video from the API, modifies its name to
    give the date and time taken, and saves it to a folder based on the one it matched in
    Google Photos. Any media that is not organized automatically gets put in a "Not Organized"
    folder. Additionally, photos that do not have EXIF data will automatically have data 
    generated.
  
    Parameters
    ----------
    photo (dict) : Dictionary returned by the Google Photos API.
    session (aiohttp.ClientSession) : Aiohttp session object for async HTTP requests.
    """

    no_exif = False
    filename = photo['filename']
    file_extension = filename[filename.rfind('.'):].lower()
    folder_name = photo['album']
    creation_time = photo['mediaMetadata']['creationTime']
    is_video = (folder_name == "Videos" or file_extension == '.mov' or file_extension == '.mp4')
    actual_date = utc_to_pt(creation_time)

    #The '=dv' and '=d' download the videos and photos respectively in full quality and properly
    if (is_video):
        media_url = photo['baseUrl'] + '=dv'
    else:
        media_url = photo['baseUrl'] + '=d'
 
    async with session.get(media_url) as response:

        photo_bytes = await response.content.read()

    if (folder_name == "Not in any album"):
        list_no_album.append(datetime.strptime(actual_date,"%Y:%m:%d %H:%M:%S").strftime(TIME_NAME_FORMAT) + file_extension.upper())
        folder_name = 'Not Organized'

    #Just for downloading it to the correct folder. Name will be changed at end of function.
    file_original_path = FILE_DIR + folder_name + '\\' + filename

    with open(file_original_path, 'wb') as f:
        f.write(photo_bytes)

        if (not is_video):
            im = Image.open(file_original_path)

            try:
                exif_dict = im.info['exif']
            except KeyError:  # Some photos may not have EXIF data
                list_no_exif.append((datetime.strptime(actual_date,"%Y:%m:%d %H:%M:%S").strftime(TIME_NAME_FORMAT) + file_extension.upper(), actual_date))
                no_exif = True
            else:
                # Apply correct date from metadata
                exif_dict = piexif.load(exif_dict)
                piexif.remove(file_original_path)
                encoded_date = actual_date.encode('utf-8')
                exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = encoded_date
                exif_dict['Exif'][piexif.ExifIFD.DateTimeDigitized] = encoded_date

                # Apply changes to photos
                exif_bytes = piexif.dump(exif_dict)
                piexif.insert(exif_bytes, file_original_path)
    
    if (no_exif):
        exif_dict = {
            "0th": {},
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: actual_date,
                piexif.ExifIFD.DateTimeDigitized: actual_date
            },
            "GPS": {},
            "Interop": {},
            "1st": {},
            "thumbnail": None
        }
        exif_bytes = piexif.dump(exif_dict)
        im.save(FILE_DIR + folder_name + '\\' +filename, exif=exif_bytes)
        im.close()

        #Only way to properly change the PNG time which is used by Windows instead of the new EXIF for some reason.
        if(filename.find("PNG")):
            subprocess.run(["exiftool", "-overwrite_original", f"-PNG:CreationTime={actual_date}", file_original_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if(not is_video):
        im.close()

    actual_date = datetime.strptime(actual_date,"%Y:%m:%d %H:%M:%S").strftime(TIME_NAME_FORMAT)

    # The following uses replace instead of rename since replace accounts for already existing files
    os.replace(file_original_path, FILE_DIR +  folder_name + '\\' + actual_date + file_extension)

async def save_medias(photos:list[dict]) -> None:
    """
    Function for saving photos/videos from API to their corresponding folder.
  
    Async function that downloads photos/videos from the API. Uses a "pool" of tasks to 
    concurrently download the media without them blocking eachother
  
    Parameters
    ----------
    photo (list[dict]) : List of dictionaries which are returned by the Google Photos API.
    """

    async with aiohttp.ClientSession() as session:
        tasks = []

        for photo in photos:
            task = asyncio.ensure_future(save_media(photo,session))
            tasks.append(task)

        await tqdm_asyncio.gather(*tasks, desc="Downloading Photos")


#AUTH Code Found Here: https://stackoverflow.com/questions/58928685/google-photos-api-python-working-non-deprecated-example

credentialsFile = 'credentials.json' 
pickleFile = 'token.pickle' 

SCOPES = ['https://www.googleapis.com/auth/photoslibrary']
creds = None
if os.path.exists(pickleFile):
    with open(pickleFile, 'rb') as token:
        creds = pickle.load(token)
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            credentialsFile, SCOPES)
        creds = flow.run_local_server()
    with open(pickleFile, 'wb') as token:
        pickle.dump(creds, token)

service = build('photoslibrary', 'v1', credentials=creds, static_discovery=False)

#Patch for albumId not being a valid arg: https://github.com/googleapis/google-api-python-client/issues/733
search_params = {
  "albumId": {
    "description": "Identifier of an album. If populated, lists all media items in specified album. Can't set in conjunction with any filters.",
    "location": "query",
    "type": "string"
  },
  "pageSize": {
    "description": "Maximum number of media items to return in the response. Fewer media items might be returned than the specified number. The default pageSize is 25, the maximum is 100.",
    "location": "query",
    "type": "integer",
    "format": "int32"
  },
  "pageToken": {
    "description": "A continuation token to get the next page of the results. Adding this to the request returns the rows after the pageToken. The pageToken should be the value returned in the nextPageToken parameter in the response to the searchMediaItems request.",
    "location": "query",
    "type": "string"
  },
  "filters": {
    "description": "Filters to apply to the request. Can't be set in conjunction with an albumId.",
    "location": "query",
    "type": "object"
  }
}

service._resourceDesc['resources']['mediaItems']['methods']['search']['parameters'].update(search_params)

start_date = datetime(2023,8,25)

photos = [content for content in get_media(service,{'pageSize':20},start_date)]

request = service.albums().list(pageSize=30) #Only need 30 items since there are only 28 albums currently. This will most likely be changed in favour of a generator.
response = request.execute()

albums = response['albums']

#print(albums)

album_id_list = [album['id'] for album in albums]

#print(album_id_list)

album_media = []

start = time.time()
#Grab all album media, satisfying the start date, for each album using multithreading.
with tqdm(total=len(albums), desc = "-> Grabbing album media...") as pbar:
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [executor.submit(get_album_thread, creds,album,start_date) for album in albums]

        # Wait for all tasks to finish
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            album_media.append(result)
            pbar.update(1)
end = time.time()
print(f"Album media download time: {end - start}s")

#The album_media is out of order so we can use the album_id_list to reorder it based on the id 
album_media = sorted(album_media, key=lambda element: album_id_list.index(element[0]))
# print(album_media)

#Don't need the album_id anymore.
for list_element in album_media:
    del list_element[0]

album_titles = [] #List of album titles that a certain photo belongs to.

#Iterate through all albums and check which ones a photo belongs to.
for i in range(len(photos)+1): # Iterate through each photo.

    #Add the list of album titles that a photo is in to a key
    #Without the i > 0 the the entire list would be shifted
    if(album_titles and i > 0):
        photos[i-1].update({'album':album_titles})
    elif (i > 0): 
        photos[i-1].update({'album':"Not in any album"})

    album_titles = [] #Reset after each iteration

    #Required since the last iteration will be skipped (hence the len(photos)+1 in the above).
    if(i == len(photos)):
        break

    for j in range(len(albums)): # Iterate through each album
        
        #Find out if photo is in the album
        for dic in album_media[j]:
            if dic['id'] == photos[i]['id']:
                album_titles.append(albums[j]['title'])
                break

        #Takes care of special cases where we can just end the iteration
        if(album_titles.count('Random People') or album_titles.count('Unspecified')):
            break
        elif(album_titles.count('Videos') or len(album_titles) >= 3):
            album_titles = ['Videos']
            break

#Takes care of duo photos here instead of the loop due to ordering issues
for photo in photos:
    titles = photo['album']
    if(len(titles) == 2):
        photo['album'] = "Group Stuff"
    elif(type(titles) is list):
        photo['album'] = titles[0]
    else:
        photo['album'] = titles

print(photos[0])

start = time.time()
loop = asyncio.get_event_loop()
future = asyncio.ensure_future(save_medias(photos))
results = loop.run_until_complete(future)
end = time.time()

print(f"Photo Download Time {end-start}s")

if(list_no_exif):
    print(f"The following image{'s' if (len(list_no_exif) > 1) else ''} had no EXIF data and data was created automatically:")
    for i in range(len(list_no_exif)):
        print(list_no_exif[i][0] + "-> Date Taken: " + list_no_exif[i][1])
if(list_no_album):
    print(f"The following image{'s' if (len(list_no_album) > 1) else ''} did not belong to any album and {'were' if (len(list_no_album) > 1) else 'was'} not organized:")
    for i in range(len(list_no_album)):
        print(list_no_album[i])
print("NOTE: PNG images may use the tag of CreationTime instead of the supplied EXIF. This is accounted for.")