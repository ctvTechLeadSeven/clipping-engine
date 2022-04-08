#  Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

import os
import json
import uuid
import boto3
from chalice import BadRequestError, NotFoundError

PROFILE_TABLE_NAME = os.environ['PROFILE_TABLE_NAME']
MEDIASOURCE_S3_BUCKET = os.environ['MEDIASOURCE_S3_BUCKET']
SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']

medialive_client = boto3.client("medialive")
ddb_resource = boto3.resource("dynamodb")
cw_client = boto3.client("cloudwatch")
sqs_client = boto3.client("sqs")

def add_or_update_medialive_output_group(name, program, profile, channel_id):
    print(
        f"Describing the MediaLive channel '{channel_id}' to get existing InputAttachments, Destinations, and EncoderSettings")

    try:
        response = medialive_client.describe_channel(
            ChannelId=channel_id
        )

        if response["State"] != "IDLE":
            raise BadRequestError(f"MediaLive channel '{channel_id}' is not in 'IDLE' state")

        last_known_medialive_config = response
        input_attachments = response["InputAttachments"]
        destinations = response["Destinations"]
        encoder_settings = response["EncoderSettings"]

        profile_table = ddb_resource.Table(PROFILE_TABLE_NAME)

        response = profile_table.get_item(
            Key={
                "Name": profile
            },
            ProjectionExpression="#Name, #ChunkSize",
            ExpressionAttributeNames={
                "#Name": "Name",
                "#ChunkSize": "ChunkSize"
            },
            ConsistentRead=True
        )

        if "Item" not in response:
            raise NotFoundError(f"Profile '{profile}' not found")

        chunk_size = response["Item"]["ChunkSize"]

        is_destination_create = True

        for index, destination in enumerate(destinations):
            if destination["Id"] == "awsmre":
                print(f"Updating the existing MRE destination present in the MediaLive channel '{channel_id}'")
                destinations[index]["Settings"][0][
                    "Url"] = f"s3ssl://{MEDIASOURCE_S3_BUCKET}/{channel_id}/{program}/{name}/{profile}/{program}_{name}"
                is_destination_create = False
                break

        if is_destination_create:
            print(f"Creating a new destination for MRE in the MediaLive channel '{channel_id}'")

            mre_destination = {
                "Id": "awsmre",
                "Settings": [
                    {
                        "Url": f"s3ssl://{MEDIASOURCE_S3_BUCKET}/{channel_id}/{program}/{name}/{profile}/{program}_{name}"
                    }
                ]
            }

            # Append MRE destination to the existing channel destinations
            destinations.append(mre_destination)

        audio_descriptions = encoder_settings["AudioDescriptions"] if "AudioDescriptions" in encoder_settings else []

        if not audio_descriptions:
            # At this time, MRE automatically picks the first input attached to the MediaLive channel 
            # to get the AudioSelectors information. In a future update, this input picking could be user driven
            audio_selectors = input_attachments[0]["InputSettings"]["AudioSelectors"] if "AudioSelectors" in \
                                                                                         input_attachments[0][
                                                                                             "InputSettings"] else []
            audio_selectors_name_list = [audio_selector["Name"] for audio_selector in audio_selectors]

            for audio_selector_name in audio_selectors_name_list:
                audio_descriptions.append(
                    {
                        "AudioSelectorName": audio_selector_name,
                        "AudioTypeControl": "FOLLOW_INPUT",
                        "LanguageCodeControl": "FOLLOW_INPUT",
                        "Name": f"audio_{uuid.uuid4().hex}"
                    }
                )

            # Include AudioDescriptions in the EncoderSettings
            encoder_settings["AudioDescriptions"] = audio_descriptions

        audio_description_name_list = [audio_description["Name"] for audio_description in audio_descriptions]

        output_groups = encoder_settings["OutputGroups"]
        is_new_output_group = True

        for index, output_group in enumerate(output_groups):
            if "HlsGroupSettings" in output_group["OutputGroupSettings"] and \
                    output_group["OutputGroupSettings"]["HlsGroupSettings"]["Destination"][
                        "DestinationRefId"] == "awsmre":
                print(f"Updating the existing OutputGroup for MRE in the MediaLive channel '{channel_id}'")

                output_groups[index]["OutputGroupSettings"]["HlsGroupSettings"]["SegmentLength"] = int(chunk_size)
                output_groups[index]["OutputGroupSettings"]["HlsGroupSettings"]["ProgramDateTimePeriod"] = int(
                    chunk_size)

                output_groups[index]["Outputs"][0]["AudioDescriptionNames"] = audio_description_name_list

                is_new_output_group = False
                break

        if is_new_output_group:
            mre_output_group = {
                "OutputGroupSettings": {
                    "HlsGroupSettings": {
                        "AdMarkers": [],
                        "CaptionLanguageMappings": [],
                        "CaptionLanguageSetting": "OMIT",
                        "ClientCache": "ENABLED",
                        "CodecSpecification": "RFC_4281",
                        "Destination": {
                            "DestinationRefId": "awsmre"
                        },
                        "DirectoryStructure": "SINGLE_DIRECTORY",
                        "DiscontinuityTags": "INSERT",
                        "HlsId3SegmentTagging": "DISABLED",
                        "IFrameOnlyPlaylists": "DISABLED",
                        "IncompleteSegmentBehavior": "AUTO",
                        "IndexNSegments": 10,
                        "InputLossAction": "PAUSE_OUTPUT",
                        "IvInManifest": "INCLUDE",
                        "IvSource": "FOLLOWS_SEGMENT_NUMBER",
                        "KeepSegments": 21,
                        "ManifestCompression": "NONE",
                        "ManifestDurationFormat": "FLOATING_POINT",
                        "Mode": "VOD",
                        "OutputSelection": "VARIANT_MANIFESTS_AND_SEGMENTS",
                        "ProgramDateTime": "INCLUDE",
                        "ProgramDateTimePeriod": int(chunk_size),
                        "RedundantManifest": "DISABLED",
                        "SegmentLength": int(chunk_size),
                        "SegmentationMode": "USE_SEGMENT_DURATION",
                        "SegmentsPerSubdirectory": 10000,
                        "StreamInfResolution": "INCLUDE",
                        "TimedMetadataId3Frame": "PRIV",
                        "TimedMetadataId3Period": 10,
                        "TsFileMode": "SEGMENTED_FILES"
                    }
                },
                "Outputs": [
                    {
                        "AudioDescriptionNames": audio_description_name_list,
                        "CaptionDescriptionNames": [],
                        "OutputName": "awsmre",
                        "OutputSettings": {
                            "HlsOutputSettings": {
                                "H265PackagingType": "HVC1",
                                "HlsSettings": {
                                    "StandardHlsSettings": {
                                        "AudioRenditionSets": "program_audio",
                                        "M3u8Settings": {
                                            "AudioFramesPerPes": 4,
                                            "AudioPids": "492-498",
                                            "NielsenId3Behavior": "NO_PASSTHROUGH",
                                            "PcrControl": "PCR_EVERY_PES_PACKET",
                                            "PmtPid": "480",
                                            "ProgramNum": 1,
                                            "Scte35Behavior": "NO_PASSTHROUGH",
                                            "Scte35Pid": "500",
                                            "TimedMetadataBehavior": "NO_PASSTHROUGH",
                                            "TimedMetadataPid": "502",
                                            "VideoPid": "481"
                                        }
                                    }
                                },
                                "NameModifier": "_1"
                            }
                        },
                        "VideoDescriptionName": "video_awsmre"
                    }
                ]
            }

            # Append MRE output group to the existing channel output groups
            output_groups.append(mre_output_group)

        encoder_settings["OutputGroups"] = output_groups

        video_descriptions = encoder_settings["VideoDescriptions"]
        is_new_video_description = True

        for index, video_description in enumerate(video_descriptions):
            if video_description["Name"] == "video_awsmre":
                print(
                    f"Skipping the addition of new video description for MRE as it already exists in the MediaLive channel '{channel_id}'")
                is_new_video_description = False
                break

        if is_new_video_description:
            mre_video_description = {
                "CodecSettings": {
                    "H264Settings": {
                        "AdaptiveQuantization": "AUTO",
                        "AfdSignaling": "NONE",
                        "Bitrate": 5000000,
                        "BufSize": 5000000,
                        "ColorMetadata": "INSERT",
                        "EntropyEncoding": "CABAC",
                        "FlickerAq": "ENABLED",
                        "ForceFieldPictures": "DISABLED",
                        "FramerateControl": "INITIALIZE_FROM_SOURCE",
                        "GopBReference": "DISABLED",
                        "GopClosedCadence": 1,
                        "GopNumBFrames": 2,
                        "GopSize": 1,
                        "GopSizeUnits": "SECONDS",
                        "Level": "H264_LEVEL_AUTO",
                        "LookAheadRateControl": "MEDIUM",
                        "MaxBitrate": 5000000,
                        "NumRefFrames": 1,
                        "ParControl": "INITIALIZE_FROM_SOURCE",
                        "Profile": "HIGH",
                        "QvbrQualityLevel": 8,
                        "RateControlMode": "QVBR",
                        "ScanType": "PROGRESSIVE",
                        "SceneChangeDetect": "DISABLED",
                        "SpatialAq": "ENABLED",
                        "SubgopLength": "FIXED",
                        "Syntax": "DEFAULT",
                        "TemporalAq": "ENABLED",
                        "TimecodeInsertion": "DISABLED"
                    }
                },
                "Name": "video_awsmre",
                "RespondToAfd": "NONE",
                "ScalingBehavior": "DEFAULT",
                "Sharpness": 50
            }

            # Append MRE video description to the existing channel video descriptions
            video_descriptions.append(mre_video_description)

        encoder_settings["VideoDescriptions"] = video_descriptions

        # Update the MediaLive channel with modified Destinations and EncoderSettings
        print(f"Updating the MediaLive channel '{channel_id}' with modified Destinations and EncoderSettings")
        medialive_client.update_channel(
            ChannelId=channel_id,
            Destinations=destinations,
            EncoderSettings=encoder_settings
        )

    except medialive_client.exceptions.NotFoundException as e:
        print(f"MediaLive channel '{channel_id}' not found")
        raise Exception(e)

    except Exception as e:
        print(
            f"Unable to add a new or update an existing OutputGroup for MRE in the MediaLive channel '{channel_id}': {str(e)}")
        raise Exception(e)

    else:
        return last_known_medialive_config


def create_cloudwatch_alarm_for_channel(channel_id):
    print(
        f"Adding/Updating the CloudWatch Alarm for 'InputVideoFrameRate' metric for the MediaLive channel '{channel_id}'")

    try:
        cw_client.put_metric_alarm(
            AlarmName=f"AWS_MRE_MediaLive_{channel_id}_InputVideoFrameRate_Alarm",
            AlarmDescription=f"Alarm created by AWS MRE for the MediaLive channel {channel_id} to monitor input video frame rate and update the status of an Event to Complete",
            ComparisonOperator="LessThanOrEqualToThreshold",
            MetricName="InputVideoFrameRate",
            Period=10,
            EvaluationPeriods=1,
            DatapointsToAlarm=1,
            Threshold=0.0,
            TreatMissingData="breaching",
            Namespace="MediaLive",
            Statistic="Minimum",
            Dimensions=[
                {
                    "Name": "ChannelId",
                    "Value": channel_id
                },
                {
                    "Name": "Pipeline",
                    "Value": "0"
                }
            ],
            ActionsEnabled=False,
            Tags=[
                {
                    "Key": "Project",
                    "Value": "MRE"
                }
            ]
        )

    except Exception as e:
        print(
            f"Unable to add or update the CloudWatch Alarm for 'InputVideoFrameRate' metric for the MediaLive channel '{channel_id}': {str(e)}")
        raise Exception(e)

def delete_medialive_output_group(name, program, profile, channel_id):
    print(f"Describing the MediaLive channel '{channel_id}' to get existing Destinations and EncoderSettings")

    try:
        response = medialive_client.describe_channel(
            ChannelId=channel_id
        )

        if response["State"] != "IDLE":
            print(
                f"Skipping deletion of MRE Destination and OutputGroup as the MediaLive channel '{channel_id}' is not in 'IDLE' state")
            return

        destinations = response["Destinations"]
        encoder_settings = response["EncoderSettings"]

        delete_output_group = False

        for index, destination in enumerate(destinations):
            if destination["Id"] == "awsmre" and destination["Settings"][0][
                "Url"] == f"s3ssl://{MEDIASOURCE_S3_BUCKET}/{channel_id}/{program}/{name}/{profile}/{program}_{name}":
                print(f"Deleting the MRE destination present in the MediaLive channel '{channel_id}'")
                destinations.pop(index)
                delete_output_group = True
                break

        if delete_output_group:
            output_groups = encoder_settings["OutputGroups"]

            for index, output_group in enumerate(output_groups):
                if "HlsGroupSettings" in output_group["OutputGroupSettings"] and \
                        output_group["OutputGroupSettings"]["HlsGroupSettings"]["Destination"][
                            "DestinationRefId"] == "awsmre":
                    print(f"Deleting the OutputGroup for MRE in the MediaLive channel '{channel_id}'")
                    output_groups.pop(index)
                    break

            encoder_settings["OutputGroups"] = output_groups

            video_descriptions = encoder_settings["VideoDescriptions"]

            for index, video_description in enumerate(video_descriptions):
                if video_description["Name"] == "video_awsmre":
                    print(f"Deleting the VideoDescription for MRE in the MediaLive channel '{channel_id}'")
                    video_descriptions.pop(index)
                    break

            encoder_settings["VideoDescriptions"] = video_descriptions

            # Update the MediaLive channel with modified Destinations and EncoderSettings
            print(f"Updating the MediaLive channel '{channel_id}' with modified Destinations and EncoderSettings")
            medialive_client.update_channel(
                ChannelId=channel_id,
                Destinations=destinations,
                EncoderSettings=encoder_settings
            )

        else:
            print(
                f"No deletion required as the Destination and OutputGroup for MRE are not found in the MediaLive channel '{channel_id}'")

    except medialive_client.exceptions.NotFoundException as e:
        print(f"Unable to delete the Destination and OutputGroup for MRE: MediaLive channel '{channel_id}' not found")

    except Exception as e:
        print(
            f"Unable to delete the Destination and OutputGroup for MRE in the MediaLive channel '{channel_id}': {str(e)}")

def delete_cloudwatch_alarm_for_channel(channel_id):
    print(f"Deleting the CloudWatch Alarm for 'InputVideoFrameRate' metric for the MediaLive channel '{channel_id}'")

    try:
        cw_client.delete_alarms(
            AlarmNames=[f"AWS_MRE_MediaLive_{channel_id}_InputVideoFrameRate_Alarm"]
        )

    except Exception as e:
        print(
            f"Unable to delete the CloudWatch Alarm for 'InputVideoFrameRate' metric for the MediaLive channel '{channel_id}': {str(e)}")


def notify_event_deletion_queue(name, program, profile):
    print(
        f"Sending a message to the SQS queue '{SQS_QUEUE_URL}' to notify the deletion of event '{name}' in program '{program}'")

    try:
        message = {
            "Event": name,
            "Program": program,
            "Profile": profile
        }

        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message),
        )

    except Exception as e:
        print(
            f"Unable to send a message to the SQS queue '{SQS_QUEUE_URL}' to notify the deletion of event '{name}' in program '{program}': {str(e)}")